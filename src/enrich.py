"""
For each company in the database without enrichment data, fetches the website
homepage, sends visible text to Claude via forced tool_use, and stores the
extracted fields back in the database.

Run:
    python src/enrich.py

Re-runs are idempotent — companies with enriched_at set are skipped.
To retry a failed company, clear its enriched_at field in the database.
"""

import json
import logging
import os
import re
import signal
import sqlite3
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from duckduckgo_search import DDGS

load_dotenv()

import thesis  # noqa: E402  (thesis.py is in the same src/ directory)
from db.migrate import migrate as migrate_database  # noqa: E402
from collectors.name_cleaner import strip_legal_boilerplate  # noqa: E402
from collectors.vocabulary import (  # noqa: E402
    get_search_queries,
    get_sector_terms,
    map_category_to_sector,
)
from enrich_fallback import (  # noqa: E402
    merge_contacts,
    merge_founders,
    tier2_path_walk,
    tier3_linkedin_search,
)
from logging_config import setup_logging, summary_path  # noqa: E402

log = setup_logging("enrichment")

# ---------------------------------------------------------------------------
# DDG backoff + circuit breaker
# ---------------------------------------------------------------------------

_DDG_RETRY_DELAYS = [30, 60, 120]   # seconds; 3 attempts on rate limit
_RATE_LIMIT_HINTS = ("ratelimit", "rate limit", "202 ", "429", "too many requests")

_consecutive_failures = 0
_consecutive_failure_type: str | None = None
_CIRCUIT_BREAKER_THRESHOLD = 10
_CIRCUIT_BREAKER_PAUSE = 5 * 60      # 5 minutes


def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(hint in msg for hint in _RATE_LIMIT_HINTS)


def _ddg_search_with_backoff(query: str, max_results: int = 5) -> list[dict]:
    """Run a DDG text() search with exponential backoff on rate limits.

    On rate limit: waits 30s, 60s, 120s (3 attempts).
    On any other error: logs and returns [] (no retry).
    """
    for attempt, delay in enumerate([0] + _DDG_RETRY_DELAYS):
        if delay:
            log.warning("DDG rate-limited, sleeping %ds before retry %d/%d",
                        delay, attempt, len(_DDG_RETRY_DELAYS))
            time.sleep(delay)
        try:
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=max_results)
            return list(results or [])
        except Exception as exc:
            if _is_rate_limit(exc):
                if attempt < len(_DDG_RETRY_DELAYS):
                    continue
                log.error("DDG rate limit persisted after %d retries (query=%r)",
                          len(_DDG_RETRY_DELAYS), query)
                _record_failure("ddg_rate_limit")
                return []
            log.warning("DDG search failed (query=%r): %s", query, exc)
            _record_failure("ddg_other")
            return []
    return []


def _record_failure(kind: str) -> None:
    """Track consecutive same-type failures; pause if circuit breaker trips."""
    global _consecutive_failures, _consecutive_failure_type
    if kind == _consecutive_failure_type:
        _consecutive_failures += 1
    else:
        _consecutive_failure_type = kind
        _consecutive_failures = 1
    if _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
        log.error("PAUSING %dmin — %d consecutive %s failures detected.",
                  _CIRCUIT_BREAKER_PAUSE // 60, _consecutive_failures, kind)
        time.sleep(_CIRCUIT_BREAKER_PAUSE)
        _consecutive_failures = 0
        _consecutive_failure_type = None


def _record_success() -> None:
    global _consecutive_failures, _consecutive_failure_type
    _consecutive_failures = 0
    _consecutive_failure_type = None

DB_PATH = Path(__file__).parent.parent / "data" / "companies.db"

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_MAX_TEXT_CHARS = 8_000    # per-page cap; keeps individual fetches cheap
_MAX_COMBINED_CHARS = 15_000  # total cap across homepage + search sources sent to Claude
_FETCH_TIMEOUT = 20

# Domains that are NOT company homepages — skip when discovering websites.
_SKIP_DOMAINS = {
    "linkedin.com", "crunchbase.com", "angel.co", "twitter.com", "x.com",
    "facebook.com", "instagram.com", "youtube.com", "pitchbook.com",
    "dealroom.co", "owler.com", "tracxn.com", "f6s.com", "producthunt.com",
    "bloomberg.com", "reuters.com", "techcrunch.com", "forbes.com",
    "businesswire.com", "prnewswire.com", "globenewswire.com",
    "wikipedia.org", "wikidata.org", "microsoft.com",
    "merriam-webster.com", "dictionary.com", "wiktionary.org", "thesaurus.com",
}

# Domain fragments that signal news/blog sites, not company homepages.
_SKIP_DOMAIN_FRAGMENTS = {"news", "blog", "article", "magazine", "press", "media"}

# Domain TLD suffixes to try when guessing a company's website.
# .ua added for Ukrainian companies from ProZorro.
_GUESS_TLDS = [".com", ".io", ".ai", ".tech", ".co", ".ua"]

_HEAD_TIMEOUT = 8

# Forced-tool-use schema — Claude MUST call this tool, giving reliable structured output.
_EXTRACT_TOOL = {
    "name": "extract_company_info",
    "description": (
        "Extract structured information about a defense/technology company "
        "from its homepage text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "1–2 sentence plain-English summary of what the company does.",
            },
            "product_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Product categories relevant to this company, e.g. "
                    "['UAV', 'loitering munition', 'electronic warfare', "
                    "'counter-drone', 'naval drone', 'ground robot']."
                ),
            },
            "primary_sector": {
                "type": "string",
                "enum": ["defense", "dual-use", "civilian"],
                "description": "Primary market sector.",
            },
            "hq_country": {
                "type": "string",
                "description": "Country of headquarters, e.g. 'Ukraine'.",
            },
            "founded_year": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "description": "Four-digit year founded, or null if not determinable.",
            },
            "employee_count_est": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Estimated headcount band, e.g. '50–200', or null.",
            },
            "technologies": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Key technologies used, e.g. "
                    "['AI targeting', 'EO/IR', 'GNSS-denied navigation', 'swarm coordination']."
                ),
            },
            "notable_products": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Named product models or platforms found on the page.",
            },
            "linkedin_url": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": (
                    "Company LinkedIn URL if present in the source text "
                    "(format: https://www.linkedin.com/company/...). Null if not found."
                ),
            },
            "funding": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": (
                    "One-line summary of funding history if mentioned in the source, "
                    "e.g. 'Series A $15M led by Andreessen Horowitz, 2024'. "
                    "Null if no funding info is present."
                ),
            },
            "founders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":          {"type": "string"},
                        "role":          {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "linkedin_url":  {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "bio":           {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "background":    {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "confidence":    {"type": "number"},
                    },
                    "required": ["name", "confidence"],
                },
                "description": (
                    "Founders/co-founders explicitly named in the source. "
                    "Only include people whose names appear verbatim. Do NOT fabricate. "
                    "Per-person confidence: 1.0 if directly stated as founder, "
                    "0.7 if inferred from role/bio, 0.3 if speculative."
                ),
            },
            "contacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "general_email", "sales_email", "phone",
                                "twitter", "linkedin",
                            ],
                        },
                        "value":      {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["type", "value", "confidence"],
                },
                "description": (
                    "Contact info that appears VERBATIM in the source. "
                    "Do NOT fabricate or guess emails from domain names. "
                    "Confidence: 1.0 if directly listed, 0.5 if inferred."
                ),
            },
            "field_confidence": {
                "type": "object",
                "properties": {
                    "description":  {"type": "number"},
                    "hq_country":   {"type": "number"},
                    "founded_year": {"type": "number"},
                    "funding":      {"type": "number"},
                    "founders":     {"type": "number"},
                },
                "required": ["description", "hq_country", "founded_year",
                             "funding", "founders"],
                "description": (
                    "Per-field confidence 0.0-1.0. "
                    "1.0 = directly stated in source, "
                    "0.7 = inferred from context, "
                    "0.3 = speculative, "
                    "0.0 = field is null/unknown."
                ),
            },
        },
        "required": [
            "description",
            "product_types",
            "primary_sector",
            "hq_country",
            "founded_year",
            "employee_count_est",
            "technologies",
            "notable_products",
            "linkedin_url",
            "funding",
            "founders",
            "contacts",
            "field_confidence",
        ],
    },
}

_fund = thesis.fund()
_geo = _fund["geographic_focus"]
_focus_countries = ", ".join(_geo["tier_1"] + _geo["tier_2"])

_priority_sectors = thesis.priority_sectors()
_sector_context = " | ".join(
    f"{s['name']} (keywords: {', '.join(s['keywords'][:4])})"
    for s in _priority_sectors
)

_SYSTEM = (
    f"You are a defense-sector research analyst supporting {_fund['name']} ({_fund['mission']}), "
    f"a venture fund focused on defense and dual-use technology companies in "
    f"{_focus_countries} and other NATO-aligned countries. "
    f"the fund's current priority sectors are: {_sector_context}. "
    "When extracting product_types and technologies, be specific and use terminology "
    "that maps to these priority sectors where applicable. "
    "Extract factual, specific information about companies strictly from the "
    "provided source text. Do not invent or infer information that is not present. "
    "For unknown fields use null or an empty list. "
    "\n\n"
    "FOUNDERS: Only include people whose names appear verbatim in the source. "
    "Never fabricate or guess names. If no founder is named, return an empty list. "
    "\n\n"
    "CONTACTS: Only return values that appear VERBATIM in the source. "
    "Never invent email addresses from domain names. Never guess phone numbers. "
    "If contact info is not explicitly written in the source, return an empty list. "
    "\n\n"
    "FIELD_CONFIDENCE: For each of (description, hq_country, founded_year, funding, "
    "founders), score your confidence 0.0–1.0: "
    "1.0 if directly stated in the source, "
    "0.7 if inferred from clear context, "
    "0.3 if speculative, "
    "0.0 if the field is null/unknown."
)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _migrate_db(conn: sqlite3.Connection) -> None:
    """Add enrichment columns to companies table if they don't already exist.

    Also ensures founders/contacts tables and confidence columns exist by
    delegating to migrate_founders().
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(companies)")}
    new_cols = {
        "description": "TEXT",
        "product_types": "TEXT",       # JSON array stored as text
        "primary_sector": "TEXT",
        "hq_country": "TEXT",
        "founded_year": "INTEGER",
        "employee_count_est": "TEXT",
        "technologies": "TEXT",        # JSON array stored as text
        "notable_products": "TEXT",    # JSON array stored as text
        "enriched_at": "TEXT",         # datetime string; NULL means not yet enriched
        "enrich_error": "TEXT",        # non-NULL when fetch or API call failed
    }
    for col, col_type in new_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
    conn.commit()

    from collectors.migrate import migrate_founders
    migrate_founders()


def _pending(
    conn: sqlite3.Connection,
    source: str | None = None,
    company_ids: list[int] | None = None,
) -> list[sqlite3.Row]:
    # Include website IS NULL rows — website discovery happens inside run().
    # description and primary_category are fetched so _discover_website can validate candidates.
    # source is fetched so prozorro rows can be name-cleaned for search.
    #
    # When company_ids is provided, the standard "not yet enriched" guard is
    # dropped — callers (e.g. priority cohorts) may want to process a specific
    # set regardless. The other safety filters stay on.
    if company_ids:
        placeholders = ",".join("?" * len(company_ids))
        sql = (
            f"SELECT id, name, name_latin, website, description, primary_category, source "
            f"FROM companies "
            f"WHERE id IN ({placeholders}) "
            f"AND (portfolio_company IS NULL OR portfolio_company = 0) "
            f"AND (status IS NULL OR status != 'duplicate') "
            f"AND (relevance_filter IS NULL OR relevance_filter != 'filtered_out')"
        )
        return conn.execute(sql, tuple(company_ids)).fetchall()

    sql = (
        "SELECT id, name, name_latin, website, description, primary_category, source "
        "FROM companies "
        "WHERE enriched_at IS NULL "
        "AND (portfolio_company IS NULL OR portfolio_company = 0) "
        "AND (status IS NULL OR status != 'duplicate') "
        "AND (relevance_filter IS NULL OR relevance_filter != 'filtered_out')"
    )
    params: tuple = ()
    if source:
        sql += " AND source = ?"
        params = (source,)
    return conn.execute(sql, params).fetchall()


_UNKNOWN_STRINGS = {"<unknown>", "unknown", "n/a", "none", "null", "not available", "not determinable"}


def _is_unknown_sentinel(value) -> bool:
    """True iff `value` is one of Claude's "I don't know" sentinel strings."""
    if not isinstance(value, str):
        return False
    return value.strip().lower().strip("<>") in _UNKNOWN_STRINGS


def _clean_fields(value):
    """Recursively replace Claude's sentinel strings with None and drop them
    from lists.

    Recurses through dicts (including dicts inside lists), lists (including
    lists inside dicts), and leaves non-string scalars (int, None, bool,
    float) untouched. Was previously flat-only — list-of-dicts (founders,
    contacts) leaked `<UNKNOWN>` values through to the DB on every
    enrichment.
    """
    if _is_unknown_sentinel(value):
        return None
    if isinstance(value, dict):
        return {k: _clean_fields(v) for k, v in value.items()}
    if isinstance(value, list):
        # Drop sentinel-only items (matches old flat-list behaviour) and
        # recurse into every other item.
        return [_clean_fields(item) for item in value if not _is_unknown_sentinel(item)]
    return value


def _clean_fields_self_test() -> int:
    """Inline self-test for `_clean_fields`. Returns 0 on success, 1 on failure."""
    cases: list[tuple[str, object, object]] = [
        # (label, input, expected)
        ("flat: <UNKNOWN> → None",
         {"hq_country": "<UNKNOWN>", "founded_year": 2020},
         {"hq_country": None,        "founded_year": 2020}),
        ("flat: 'unknown' (no brackets) also cleaned",
         {"description": "unknown"},
         {"description": None}),
        ("list-of-strings: <UNKNOWN> dropped (legacy behaviour preserved)",
         {"product_types": ["radar", "<UNKNOWN>", "uav"]},
         {"product_types": ["radar", "uav"]}),
        ("list-of-dicts: founder with <UNKNOWN> role → None",
         {"founders": [{"name": "Ada", "role": "<UNKNOWN>", "linkedin_url": "https://x"}]},
         {"founders": [{"name": "Ada", "role": None,         "linkedin_url": "https://x"}]}),
        ("list-of-dicts: contact with <UNKNOWN> value → None",
         {"contacts": [{"type": "general_email", "value": "<UNKNOWN>"}]},
         {"contacts": [{"type": "general_email", "value": None}]}),
        ("nested dict: <UNKNOWN> at depth > 1 → None",
         {"field_confidence": {"description": 0.9, "founded_year": "<UNKNOWN>"}},
         {"field_confidence": {"description": 0.9, "founded_year": None}}),
        ("non-string scalars untouched: int, None, bool, float",
         {"founded_year": 2020, "description": None, "portfolio_company": True, "score": 0.85},
         {"founded_year": 2020, "description": None, "portfolio_company": True, "score": 0.85}),
        ("mixed: list of mixed-type items recurses correctly",
         {"items": ["keep", "<UNKNOWN>", {"k": "<UNKNOWN>", "n": 1}]},
         {"items": ["keep",              {"k": None,        "n": 1}]}),
    ]
    failures: list[str] = []
    for label, given, expected in cases:
        got = _clean_fields(given)
        if got == expected:
            print(f"  PASS  {label}")
        else:
            failures.append(f"  FAIL  {label}: got {got!r}, expected {expected!r}")
            print(failures[-1])
    n_total = len(cases)
    print(f"\n{n_total - len(failures)}/{n_total} cases passed")
    return 0 if not failures else 1


def _coerce_confidence(v) -> float | None:
    try:
        f = float(v)
        if 0.0 <= f <= 1.0:
            return f
    except (TypeError, ValueError):
        pass
    return None


def _store_founders(
    conn: sqlite3.Connection, company_id: int, founders: list[dict]
) -> int:
    """Replace any existing founder rows for this company and write new ones.
    Returns the number of rows written.

    Source-aware: rows tagged with `source_url LIKE 'apify_leads://%'` OR
    `apify_company_employees://%` are preserved across enrichment runs.
    Phase 1 (apify_leads) writes the first sentinel; Phase 3
    (apify_company_employees) writes the second. This filter prevents
    enrich from silently clobbering either source's data — see
    docs/DECISIONS.md D-003 (the pattern) and D-016 (Phase 3 extension).
    """
    conn.execute(
        "DELETE FROM founders WHERE company_id = ? "
        "AND (source_url IS NULL "
        "     OR (source_url NOT LIKE 'apify_leads://%' "
        "         AND source_url NOT LIKE 'apify_company_employees://%'))",
        (company_id,),
    )
    written = 0
    for f in founders or []:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        conn.execute(
            """
            INSERT INTO founders
                (company_id, name, role, linkedin_url, bio, background, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_id,
                name,
                f.get("role"),
                f.get("linkedin_url"),
                f.get("bio"),
                f.get("background"),
                _coerce_confidence(f.get("confidence")),
            ),
        )
        written += 1
    return written


def _store_contacts(
    conn: sqlite3.Connection, company_id: int, contacts: list[dict]
) -> int:
    """Replace any existing contact rows for this company and write new ones.
    Returns the number of rows written.

    Source-aware: rows tagged with `source_url LIKE 'apify_leads://%'` OR
    `apify_company_employees://%` are preserved across enrichment runs.
    See _store_founders for rationale (D-003 + D-016).
    """
    conn.execute(
        "DELETE FROM contacts WHERE company_id = ? "
        "AND (source_url IS NULL "
        "     OR (source_url NOT LIKE 'apify_leads://%' "
        "         AND source_url NOT LIKE 'apify_company_employees://%'))",
        (company_id,),
    )
    written = 0
    for c in contacts or []:
        ctype = (c.get("type") or "").strip()
        value = (c.get("value") or "").strip()
        if not ctype or not value:
            continue
        conn.execute(
            """
            INSERT INTO contacts (company_id, type, value, confidence)
            VALUES (?, ?, ?, ?)
            """,
            (
                company_id,
                ctype,
                value,
                _coerce_confidence(c.get("confidence")),
            ),
        )
        written += 1
    return written


# ── enriched_at / enrich_error contract ─────────────────────────────────────
#
# These two columns are MUTUALLY EXCLUSIVE — at any moment a row should
# have at most one of them set:
#
#   enriched_at  IS NOT NULL  AND  enrich_error IS NULL      → success
#   enriched_at  IS NULL      AND  enrich_error IS NOT NULL  → attempted, failed
#   enriched_at  IS NULL      AND  enrich_error IS NULL      → never attempted
#
#   (enriched_at IS NOT NULL AND enrich_error IS NOT NULL)   → INVALID
#
# The success path (`_store_result`) sets enriched_at = now() AND
# enrich_error = NULL atomically. The failure path (`_store_error`) sets
# enrich_error = <reason> AND enriched_at = NULL atomically — without
# the NULL-out, a row that previously succeeded and then fails a re-run
# would keep its old enriched_at and silently drop out of the
# `WHERE enriched_at IS NULL` "needs enrichment" queue.
#
# Future contributors: do not write enriched_at without simultaneously
# clearing enrich_error, and do not write enrich_error without
# simultaneously clearing enriched_at. Both writers below already do
# this — keep it that way.


def _store_result(conn: sqlite3.Connection, company_id: int, fields: dict) -> None:
    fields = _clean_fields(fields)
    fc = fields.get("field_confidence") or {}

    # Avoid clobbering an existing linkedin_url with NULL — only update if non-empty.
    new_linkedin = fields.get("linkedin_url")
    if new_linkedin:
        conn.execute(
            "UPDATE companies SET linkedin_url = COALESCE(linkedin_url, ?) WHERE id = ?",
            (new_linkedin, company_id),
        )

    conn.execute(
        """
        UPDATE companies SET
            description              = :description,
            product_types            = :product_types,
            primary_sector           = :primary_sector,
            hq_country               = :hq_country,
            founded_year             = :founded_year,
            employee_count_est       = :employee_count_est,
            technologies             = :technologies,
            notable_products         = :notable_products,
            funding_summary          = :funding_summary,
            description_confidence   = :description_confidence,
            hq_country_confidence    = :hq_country_confidence,
            founded_year_confidence  = :founded_year_confidence,
            funding_confidence       = :funding_confidence,
            founders_confidence      = :founders_confidence,
            enriched_at              = datetime('now'),
            enrich_error             = NULL
        WHERE id = :id
        """,
        {
            "description":             fields.get("description"),
            "product_types":           json.dumps(fields.get("product_types", [])),
            "primary_sector":          fields.get("primary_sector"),
            "hq_country":              fields.get("hq_country"),
            "founded_year":            fields.get("founded_year"),
            "employee_count_est":      fields.get("employee_count_est"),
            "technologies":            json.dumps(fields.get("technologies", [])),
            "notable_products":        json.dumps(fields.get("notable_products", [])),
            "funding_summary":         fields.get("funding"),
            "description_confidence":  _coerce_confidence(fc.get("description")),
            "hq_country_confidence":   _coerce_confidence(fc.get("hq_country")),
            "founded_year_confidence": _coerce_confidence(fc.get("founded_year")),
            "funding_confidence":      _coerce_confidence(fc.get("funding")),
            "founders_confidence":     _coerce_confidence(fc.get("founders")),
            "id":                      company_id,
        },
    )
    n_founders = _store_founders(conn, company_id, fields.get("founders") or [])
    n_contacts = _store_contacts(conn, company_id, fields.get("contacts") or [])
    conn.commit()
    log.info("Stored %d founder(s) and %d contact(s) for company %d",
             n_founders, n_contacts, company_id)


def _store_error(conn: sqlite3.Connection, company_id: int, error: str) -> None:
    # Enforce the enriched_at/enrich_error XOR contract documented above:
    # always NULL out enriched_at when recording a failure, so a row that
    # previously succeeded re-enters the "needs enrichment" queue.
    conn.execute(
        "UPDATE companies SET enrich_error = ?, enriched_at = NULL WHERE id = ?",
        (error, company_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Website discovery helpers
# ---------------------------------------------------------------------------


def _looks_like_homepage(url: str) -> bool:
    """Return True if the URL is plausibly a company homepage (not a directory or article)."""
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower().removeprefix("www.")

        if any(skip in domain for skip in _SKIP_DOMAINS):
            return False
        if any(frag in domain for frag in _SKIP_DOMAIN_FRAGMENTS):
            return False

        path_parts = [p for p in parsed.path.strip("/").split("/") if p]

        # More than one path segment → not a homepage
        if len(path_parts) > 1:
            return False

        # Single path segment: reject article-style slugs
        if path_parts:
            seg = path_parts[0]
            if len(seg) > 40:
                return False
            if seg.count("-") >= 4:
                return False

        return True
    except Exception:
        return False


def _name_to_slug(company_name: str) -> str:
    """Lowercase, strip punctuation, collapse spaces — suitable for domain guessing."""
    slug = company_name.lower()
    slug = re.sub(r"[^a-z0-9\s]", "", slug)
    slug = re.sub(r"\s+", "", slug)
    return slug


def _probe_url(url: str) -> bool:
    """Return True if the URL responds with a 2xx or 3xx status (site exists)."""
    try:
        resp = requests.head(url, headers=_FETCH_HEADERS, timeout=_HEAD_TIMEOUT,
                             allow_redirects=True)
        return resp.status_code < 400
    except Exception:
        return False


_MIN_PAGE_CHARS = 200  # below this, assume parked/empty domain


def _page_matches_company(
    text: str,
    company_name: str,
    description: str | None,
    category_hint: str | None,
    name_latin: str | None = None,
) -> bool:
    """
    Return True if fetched page text plausibly belongs to this company.

    Passes if ANY of the following match (case-insensitive):
      - At least 2 tokens from the company name (or name_latin) appear in the text
      - Any word from the company's DB description appears
      - Any term from the mapped sector vocabulary appears
    """
    haystack = text.lower()

    # Condition 1: ≥2 tokens from either name form found in page.
    # name_latin is checked first — Ukrainian homepages may not contain Cyrillic
    # if they're in English, and English pages won't contain Cyrillic at all.
    for name_form in filter(None, [name_latin, company_name]):
        tokens = [
            t for t in re.split(r"[\s\-_]+", name_form.lower())
            if len(t) >= 3  # skip short tokens like "AS", "SL", "Inc"
        ]
        matched = sum(1 for t in tokens if t in haystack)
        if matched >= 2:
            return True
        if len(tokens) == 1 and matched == 1:
            return True

    # Condition 2: any keyword from the DB description
    if description:
        desc_words = [
            w for w in re.split(r"\W+", description.lower())
            if len(w) >= 5  # skip short stop-words
        ]
        if any(w in haystack for w in desc_words):
            return True

    # Condition 3: any term from the mapped sector vocabulary
    sector_key = map_category_to_sector(category_hint)
    if sector_key:
        try:
            sector_terms = get_sector_terms(sector_key)
            if any(t.lower() in haystack for t in sector_terms):
                return True
        except KeyError:
            pass

    return False


def _discover_website(
    company_name: str,
    description: str | None = None,
    category_hint: str | None = None,
    name_latin: str | None = None,
) -> str | None:
    """
    Search for a company's homepage using multiple strategies in order:

    a. DDG: "{name} official site" — tried with name_latin first (ASCII, DDG-friendly),
       then Cyrillic/primary name as fallback. Critical for ProZorro companies where the
       primary name is Cyrillic and DDG returns nothing useful.
    c. Domain guessing: slug + hyphenated forms from name_latin (preferred) or primary name.
    d. DDG: "{name}" site:linkedin.com/company — tried with both name forms.

    Each candidate URL is validated via _page_matches_company before returning.
    """
    # Prefer name_latin for search queries if it exists and differs from primary name.
    # Cyrillic names produce empty slugs and zero DDG signal.
    search_names = []
    if name_latin and name_latin.strip().lower() != company_name.strip().lower():
        search_names.append(name_latin)
    search_names.append(company_name)

    def _validate(url: str) -> str | None:
        """Fetch url; return it if content passes confidence check, else None."""
        if _is_linkedin(url):
            return url  # LinkedIn pages don't need content validation
        text = _fetch_text(url)
        if not text or len(text) < _MIN_PAGE_CHARS:
            log.debug("rejected %s (too short / no content)", url)
            return None
        if not _page_matches_company(text, company_name, description, category_hint,
                                     name_latin=name_latin):
            log.debug("rejected %s (no company signal in page)", url)
            return None
        return url

    # --- Strategy a: DDG quoted search (try each name form in order) ---
    for search_name in search_names:
        results = _ddg_search_with_backoff(f'"{search_name}" official site', max_results=6)
        for r in results:
            url = r.get("href", "")
            if url and _looks_like_homepage(url):
                validated = _validate(url)
                if validated:
                    return validated
        time.sleep(0.3)
    time.sleep(0.5)

    # --- Strategy c: domain guessing (name_latin gives usable slugs for Cyrillic names) ---
    slug_source = name_latin if name_latin else company_name
    slug = _name_to_slug(slug_source)
    words = slug_source.lower().split()
    hyphen = "-".join(re.sub(r"[^a-z0-9]", "", w) for w in words if w)
    candidates = {slug, hyphen}
    candidates.discard("")

    for base in candidates:
        for tld in _GUESS_TLDS:
            url = f"https://{base}{tld}"
            if _probe_url(url):
                validated = _validate(url)
                if validated:
                    return validated
            time.sleep(0.2)

    # --- Strategy d: LinkedIn fallback (try each name form) ---
    for search_name in search_names:
        results = _ddg_search_with_backoff(
            f'"{search_name}" site:linkedin.com/company', max_results=3
        )
        for r in results:
            url = r.get("href", "")
            if url and "linkedin.com/company" in url:
                return url

    return None


def _is_linkedin(url: str) -> bool:
    return "linkedin.com" in url.lower()


def _ensure_linkedin_col(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(companies)")}
    if "linkedin_url" not in existing:
        conn.execute("ALTER TABLE companies ADD COLUMN linkedin_url TEXT")
        conn.commit()


def _store_website(conn: sqlite3.Connection, company_id: int, url: str) -> bool:
    """
    Write a discovered URL back to companies.

    LinkedIn URLs go into linkedin_url (not website) so the enrichment loop
    can still fetch page text while keeping website reserved for the real homepage.
    Returns False on UNIQUE conflict.
    """
    try:
        if _is_linkedin(url):
            conn.execute(
                "UPDATE companies SET linkedin_url = ? WHERE id = ?", (url, company_id)
            )
        else:
            conn.execute("UPDATE companies SET website = ? WHERE id = ?", (url, company_id))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


# ---------------------------------------------------------------------------
# Fetch + extract helpers
# ---------------------------------------------------------------------------


_JINA_HEADERS = {
    "Accept": "application/json",
    "X-Return-Format": "text",
}


def _fetch_text(url: str) -> str | None:
    """Fetch a URL and return visible text, truncated to _MAX_TEXT_CHARS.

    Tries Jina Reader first (handles JS-rendered pages and bot-blocks),
    falls back to raw requests + BeautifulSoup.
    """
    # --- Primary: Jina Reader ---
    try:
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers=_JINA_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        payload = json.loads(resp.text)
        text = payload.get("data", {}).get("text", "").strip()
        if text:
            return text[:_MAX_TEXT_CHARS]
    except Exception as exc:
        log.debug("jina error for %s: %s", url, exc)

    # --- Fallback: raw requests + BeautifulSoup ---
    try:
        resp = requests.get(url, headers=_FETCH_HEADERS, timeout=_FETCH_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        if text:
            return text[:_MAX_TEXT_CHARS]
    except Exception as exc:
        log.debug("fetch error for %s: %s", url, exc)

    return None


def _gather_search_sources(
    company_name: str,
    sector_hint: str | None,
    skip_url: str | None = None,
    name_latin: str | None = None,
    max_queries: int = 4,
    max_fetches: int = 3,
) -> list[str]:
    """
    Run vocabulary-driven DDG searches and return fetched page texts.

    Uses name_latin for queries when available (ASCII-friendly for DDG).
    Skips the company's own domain and all _SKIP_DOMAINS.
    Returns up to max_fetches non-empty page texts.
    """
    search_name = name_latin if name_latin else company_name
    queries = get_search_queries(search_name, sector_hint)[:max_queries]
    log.debug("search queries: %s", queries)

    skip_domain: str | None = None
    if skip_url:
        try:
            skip_domain = urllib.parse.urlparse(skip_url).netloc.lower().removeprefix("www.")
        except Exception:
            pass

    seen_urls: set[str] = set()
    candidate_urls: list[str] = []

    for query in queries:
        results = _ddg_search_with_backoff(query, max_results=5)
        for r in results:
            url = r.get("href", "")
            if not url or url in seen_urls:
                continue
            try:
                domain = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
            except Exception:
                continue
            if any(sd in domain for sd in _SKIP_DOMAINS):
                continue
            if skip_domain and domain == skip_domain:
                continue
            seen_urls.add(url)
            candidate_urls.append(url)
        time.sleep(0.4)

    texts: list[str] = []
    for url in candidate_urls:
        if len(texts) >= max_fetches:
            break
        text = _fetch_text(url)
        if text and len(text) >= _MIN_PAGE_CHARS:
            texts.append(text)
        time.sleep(0.3)

    return texts


def _combine_texts(website_text: str | None, extra_texts: list[str]) -> str:
    """
    Concatenate homepage text and search source texts up to _MAX_COMBINED_CHARS.

    Each section is labelled so Claude knows the provenance. Homepage always comes
    first (most authoritative). Extra texts fill remaining budget in order.
    """
    sections: list[str] = []
    remaining = _MAX_COMBINED_CHARS

    if website_text:
        chunk = website_text[:remaining]
        sections.append(f"=== Homepage ===\n{chunk}")
        remaining -= len(chunk)

    for i, text in enumerate(extra_texts, start=1):
        if remaining <= 100:
            break
        chunk = text[:remaining]
        sections.append(f"=== Search Result {i} ===\n{chunk}")
        remaining -= len(chunk)

    return "\n\n".join(sections)


def _extract_via_claude(
    client: anthropic.Anthropic, company_name: str, combined_text: str
) -> dict | None:
    """Call Claude with forced tool_use to extract structured company data.

    Retries once after a 5-second delay on transient API errors.
    """
    for attempt in range(2):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[_EXTRACT_TOOL],
                tool_choice={"type": "tool", "name": "extract_company_info"},
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Company name: {company_name}\n\n"
                            f"Research sources:\n{combined_text}"
                        ),
                    }
                ],
            )
            for block in response.content:
                if block.type == "tool_use":
                    return block.input
            return None
        except anthropic.APIError as exc:
            log.warning("Claude API error (attempt %d/2): %s", attempt + 1, exc)
            if attempt == 0:
                time.sleep(5)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _write_summary(stats: dict) -> None:
    """Write a human-readable end-of-run summary to logs/."""
    path = summary_path("enrichment")
    runtime = stats["runtime_seconds"]
    processed = stats["processed"]
    successful = stats["successful"]
    avg = (runtime / processed) if processed else 0.0

    lines = [
        f"Enrichment run summary — {stats['started_at']} → {stats['ended_at']}",
        f"Total runtime:        {runtime:.1f}s ({runtime/60:.1f} min)",
        f"Total processed:      {processed}",
        f"Successful:           {successful}",
        f"Failed:               {processed - successful}",
        f"Avg time/company:     {avg:.1f}s",
        "",
        "Failure breakdown:",
    ]
    if stats["failures"]:
        for kind, count in stats["failures"].most_common():
            lines.append(f"  {kind:30s} {count}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Successful (description preview):")
    for name, desc in stats["successes"][-25:]:
        snippet = (desc or "")[:100].replace("\n", " ")
        lines.append(f"  - {name}: {snippet}")

    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Summary written to %s", path)


def _linkedin_fallback_enabled(cli_flag: bool) -> bool:
    """Tier 3 LinkedIn fallback is opt-in via CLI flag OR env var.

    Set ENRICH_LINKEDIN_FALLBACK=1 to enable from a launcher script.
    Default: OFF (Tier 2 path-walk always on; Tier 3 must be explicit).
    """
    if cli_flag:
        return True
    raw = os.environ.get("ENRICH_LINKEDIN_FALLBACK", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def run(
    limit: int | None = None,
    source: str | None = None,
    multi_source: bool = True,
    skip_scoring: bool = False,
    company_ids: list[int] | None = None,
    progress_every: int | None = None,
    linkedin_fallback: bool = False,
) -> None:
    migrate_database(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _migrate_db(conn)
    _ensure_linkedin_col(conn)

    # Load scoring modules once — avoids per-company import overhead
    _score_one = _classify_one = None
    if not skip_scoring:
        try:
            import score as _score_mod
            import classify as _classify_mod
            _score_mod._migrate_db(conn)
            _classify_mod._migrate_db(conn)
            _score_one    = _score_mod.score_one
            _classify_one = _classify_mod.classify_one
        except ImportError as exc:
            log.warning("Auto-scoring unavailable: %s", exc)

    rows = _pending(conn, source=source, company_ids=company_ids)
    if not rows:
        log.info("All companies already enriched — nothing to do.")
        conn.close()
        return

    if limit:
        rows = rows[:limit]

    client = anthropic.Anthropic()
    mode = "multi-source" if multi_source else "single-source"
    li_fb = _linkedin_fallback_enabled(linkedin_fallback)
    log.info("Enriching %d companies (%s, tier3_linkedin=%s)...",
             len(rows), mode, "ON" if li_fb else "OFF")

    stats: dict = {
        "processed": 0,
        "successful": 0,
        "failures": Counter(),
        "successes": [],
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ended_at": None,
        "runtime_seconds": 0.0,
    }
    start_time = time.time()

    def _finalize(*_args) -> None:
        stats["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        stats["runtime_seconds"] = time.time() - start_time
        _write_summary(stats)

    # Write a summary even on Ctrl-C / SIGTERM
    signal.signal(signal.SIGINT, lambda s, f: (_finalize(), exit(130)))
    signal.signal(signal.SIGTERM, lambda s, f: (_finalize(), exit(143)))

    try:
        total_n = len(rows)
        for idx, row in enumerate(rows, start=1):
            stats["processed"] += 1
            log.info("Processing [%d/%d] %s", idx, total_n, row["name"])
            if progress_every and idx % progress_every == 0:
                elapsed_min = (time.time() - start_time) / 60.0
                log.info("Enriched %d/%d (%.1f minutes elapsed)",
                         idx, total_n, elapsed_min)

            # ProZorro names are wrapped in legal-entity boilerplate that
            # destroys DDG hit rate. Strip it for search queries only —
            # the canonical row["name"] in DB stays untouched.
            search_name_primary = row["name"]
            search_name_latin   = row["name_latin"]
            if (row["source"] or "").lower() == "prozorro":
                cleaned_primary = strip_legal_boilerplate(row["name"])
                cleaned_latin   = strip_legal_boilerplate(row["name_latin"] or "")
                if cleaned_primary != row["name"] or cleaned_latin != (row["name_latin"] or ""):
                    log.info(
                        "ProZorro name cleaned: %r -> %r | latin: %r -> %r",
                        row["name"], cleaned_primary,
                        row["name_latin"], cleaned_latin,
                    )
                search_name_primary = cleaned_primary or row["name"]
                search_name_latin   = cleaned_latin or row["name_latin"]

            website = row["website"]
            website_text: str | None = None

            # --- Step 1: fetch homepage (or discover one) ---
            if website:
                log.info("Found website: %s", website)
                website_text = _fetch_text(website)
                if not website_text:
                    log.warning("Homepage fetch failed for %s", website)
            else:
                discovered = _discover_website(
                    search_name_primary,
                    description=row["description"],
                    category_hint=row["primary_category"],
                    name_latin=search_name_latin,
                )
                if discovered:
                    is_li = _is_linkedin(discovered)
                    ok = _store_website(conn, row["id"], discovered)
                    label = "linkedin" if is_li else "discovered"
                    log.info("Found website: %s (%s)%s", discovered, label,
                             "" if ok else " [conflict]")
                    if ok:
                        website = discovered
                        website_text = _fetch_text(discovered)
                        if not website_text:
                            log.warning("Fetch failed for discovered %s", discovered)
                else:
                    log.warning("Discovery failed for %s", row["name"])

            # --- Step 2: vocabulary-driven search sources ---
            extra_texts: list[str] = []
            if multi_source:
                sector_hint = map_category_to_sector(row["primary_category"])
                extra_texts = _gather_search_sources(
                    search_name_primary,
                    sector_hint=sector_hint,
                    skip_url=website,
                    name_latin=search_name_latin,
                )
            log.info("Gathered %d search sources", len(extra_texts))

            # --- Step 3: bail if nothing was collected ---
            if not website_text and not extra_texts:
                _store_error(conn, row["id"], "no content found (homepage + search both failed)")
                log.error("Skipped %s — no content found", row["name"])
                stats["failures"]["no_content"] += 1
                _record_failure("no_content")
                continue

            # --- Step 4: combine and extract ---
            combined = _combine_texts(website_text, extra_texts)
            extract_name = search_name_latin if search_name_latin else search_name_primary
            fields = _extract_via_claude(client, extract_name, combined)
            if not fields:
                _store_error(conn, row["id"], "Claude extraction returned no tool_use block")
                log.error("Extraction failed for %s", row["name"])
                stats["failures"]["claude_extraction"] += 1
                _record_failure("claude_extraction")
                continue

            # --- Step 4b: tiered fallback when Tier 1 misses founders ---
            #
            # Trigger (pattern-8 shape): Tier 1 returned no founders AND no
            # company LinkedIn URL, AND we have a real (non-LinkedIn)
            # homepage to walk. We do NOT condition on contacts — Tier 1
            # routinely surfaces a generic info@ email even when no founder
            # bios exist on the homepage, and that shouldn't suppress the
            # founder hunt.
            #
            # Tier 2: walk /about, /team, /leadership, etc. off the
            #         homepage domain; stop at the first founder/contact hit.
            # Tier 3: opt-in (--linkedin-fallback) — DDG-search LinkedIn
            #         for the firm's founder/CEO and extract.
            #
            # Fallback ADDS to fields; never overrides Tier 1's description /
            # sector / product columns. Founders/contacts merged with dedup
            # (whitespace-normalized lowercased name for founders;
            # (type, lower(value)) for contacts; higher-confidence wins).
            tier_label = "t1"
            n_fnd_t1 = len(fields.get("founders") or [])
            if (
                n_fnd_t1 == 0
                and not fields.get("linkedin_url")
                and website
                and not _is_linkedin(website)
            ):
                t2_payload = tier2_path_walk(
                    homepage_url=website,
                    claude_client=client,
                    company_name=extract_name,
                    fetch_text=_fetch_text,
                )
                if t2_payload:
                    fields["founders"] = merge_founders(
                        fields.get("founders"), t2_payload.get("founders"),
                    )
                    fields["contacts"] = merge_contacts(
                        fields.get("contacts"), t2_payload.get("contacts"),
                    )
                    for k in ("linkedin_url", "hq_country", "founded_year"):
                        if not fields.get(k) and t2_payload.get(k):
                            fields[k] = t2_payload[k]
                    tier_label = f"t2:{t2_payload.get('_path', '?')}"
                elif li_fb:
                    t3_payload = tier3_linkedin_search(
                        company_name=extract_name,
                        claude_client=client,
                        ddg_search=_ddg_search_with_backoff,
                        fetch_text=_fetch_text,
                    )
                    if t3_payload:
                        fields["founders"] = merge_founders(
                            fields.get("founders"), t3_payload.get("founders"),
                        )
                        fields["contacts"] = merge_contacts(
                            fields.get("contacts"), t3_payload.get("contacts"),
                        )
                        for k in ("linkedin_url", "hq_country", "founded_year"):
                            if not fields.get(k) and t3_payload.get(k):
                                fields[k] = t3_payload[k]
                        tier_label = "t3:linkedin"
                    else:
                        tier_label = "none"
                else:
                    tier_label = "none"

            cleaned = _clean_fields(fields)
            _store_result(conn, row["id"], fields)

            score_val = None
            tier_val = None
            if _score_one and _classify_one:
                try:
                    result = _score_one(row["id"], conn)
                    primary, tier_val = _classify_one(row["id"], conn)
                    if result:
                        score_val = result.get("total_score")
                except Exception as exc:
                    log.warning("Scoring error for %s: %s", row["name"], exc)

            field_count = sum(1 for v in cleaned.values() if v not in (None, [], ""))
            log.info("Extracted %d fields, score=%s, tier=%s, source=%s",
                     field_count,
                     f"{score_val:.2f}" if score_val is not None else "n/a",
                     tier_val if tier_val is not None else "n/a",
                     tier_label)

            stats["successful"] += 1
            stats["successes"].append((row["name"], cleaned.get("description")))
            _record_success()

            time.sleep(0.5)
    finally:
        conn.close()
        _finalize()
        total = sqlite3.connect(DB_PATH).execute(
            "SELECT COUNT(*) FROM companies WHERE enriched_at IS NOT NULL"
        ).fetchone()[0]
        log.info("Done. %d companies now enriched.", total)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Enrich companies with homepage data via Claude.")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Only process the first N pending companies.")
    ap.add_argument("--source", type=str, default=None, metavar="SRC",
                    help='Filter to companies from a specific source, e.g. "NATO DIANA 2026".')
    ap.add_argument("--single-source", action="store_true",
                    help="Fetch only the company homepage — no vocabulary-driven searches. "
                         "Faster but produces sparser results.")
    ap.add_argument("--skip-scoring", action="store_true",
                    help="Skip auto-scoring/classification after enrichment. "
                         "Useful for batch runs where you'll rescore everything at the end.")
    ap.add_argument("--linkedin-fallback", action="store_true",
                    help="Enable Tier 3 LinkedIn-search fallback when Tier 1 and "
                         "Tier 2 both return no founders/contacts. "
                         "Equivalent: ENRICH_LINKEDIN_FALLBACK=1 env var.")
    ap.add_argument("--self-test", action="store_true",
                    help="Run inline tests for _clean_fields and exit. "
                         "No DB or API access.")
    args = ap.parse_args()
    if args.self_test:
        import sys as _sys
        _sys.exit(_clean_fields_self_test())
    run(
        limit=args.limit,
        source=args.source,
        multi_source=not args.single_source,
        skip_scoring=args.skip_scoring,
        linkedin_fallback=args.linkedin_fallback,
    )
