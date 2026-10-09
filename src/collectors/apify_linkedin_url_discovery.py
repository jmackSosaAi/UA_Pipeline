"""Phase 2a — heuristic LinkedIn-URL discovery.

For each thin company without a `linkedin_url`, generate a canonical-form
slug from the name (e.g. "Atlas Space Operations Inc." → "atlas-space-
operations"), construct the candidate URL, and validate it against the
expected name + HQ country via the existing `linkedin_canonicalize`
matcher. Write the URL into `companies.linkedin_url` ONLY on MATCH.

Architecture rationale (from session prompt):
  Rather than calling a search actor to discover URLs (~$0.005/query
  with ambiguous results), generate canonical-form slugs and validate.
  The canonicalisation matcher already handles the "slug exists but
  wrong company" case (the architectural win from yesterday's session).
  So heuristic-then-validate is safe by construction — a wrong-but-real
  slug gets caught the same way collisions did, no row gets silently
  pointed at the wrong company.

Outcomes:
  MATCH      → write companies.linkedin_url, log to linkedin_url_discoveries
  NEAR_MISS  → log only; companies row unchanged (operator review queue)
  COLLISION  → log only; companies row unchanged
  STALE_SLUG → log only; companies row unchanged (LinkedIn 404 etc.)
  NOT_FOUND  → same as STALE_SLUG (harvestapi error string treated alike)
  SKIP       → log only; slug couldn't be generated (Cyrillic-only name etc.)

This module DOES NOT write to `linkedin_url_corrections` — that table is
reserved for the canonicalisation pass over EXISTING URLs. Discovery has
its own audit trail via `linkedin_url_discoveries`.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve()
ROOT = _HERE.parent.parent.parent
load_dotenv(dotenv_path=ROOT / ".env")

from .base import DB_PATH                            # noqa: E402
from .linkedin_canonicalize import (                  # noqa: E402
    BATCH_SIZE,
    COST_PER_RESULT_USD,
    CohortRow,
    Verdict,
    classify_match_with_country,
    country_from_description,
    is_valid_company_url,
    _call_harvestapi,
    _index_results_by_url,
)


# ── Phase 2d.2 search-fallback (harvestapi/linkedin-company-search) ─────────
#
# Q13 + Q29(a) resolved: when the name_heuristic slug-guess produces
# STALE_SLUG, the company likely exists on LinkedIn under a different
# slug shape. The search actor takes the company name (+ optional
# country) as a keyword and returns up to N candidate companies. Each
# candidate flows through the D-014 canonicalizer; we accept the first
# MATCH and write `companies.linkedin_url` under the D-018 uniqueness
# guard.
#
# Per the Phase 2d.2 pre-flight session (2026-05-11): Short mode lacks
# the structured `country` field D-014 needs. Full mode returns the
# same locations[].country / description shape as harvestapi/linkedin-
# company (the URL-validator), so the existing matcher works unchanged
# — only the response-shape unwrap differs (we pick the HQ-flagged
# location instead of locations[0], because the search actor returns
# multi-location arrays in arbitrary order).

SEARCH_ACTOR_ID = "harvestapi/linkedin-company-search"
SEARCH_ACTOR_START_COST_USD = 0.001
SEARCH_ACTOR_FULL_COST_USD  = 0.004
SEARCH_MAX_ITEMS = 2  # tighter cap → cheaper per-co; ≥1 verified case-by-case
DEFAULT_SEARCH_BUDGET_CAP_USD = 14.00

# Halt-on-0-MATCH window per Phase 2d.2 spec.
SEARCH_HALT_WINDOW = 100


# ── Phase 2d.3 Cyrillic brand-name extraction ───────────────────────────────
#
# Prozorro / brave1_articles / NATO DIANA seed rows often arrive with full
# Ukrainian or Russian legal form wrappers ('ТОВ "ВІЗАРДЛАБ"',
# 'ПрАТ «Київстар»', or a slash-appended beneficial-owner disclosure
# paragraph). harvestapi/linkedin-company-search handles Cyrillic input
# correctly when the QUERY is the brand-only form; raw input with
# legal prefixes finds nothing.
#
# Legal prefixes (case-insensitive, leading position, optional trailing
# punctuation): Ukrainian ТОВ / ТзОВ / ПрАТ / ПАТ / ДП / ВАТ / ПП / ФОП
# (Товариство з обмеженою відповідальністю / Приватне акціонерне товариство
# / Публічне акціонерне товариство / Державне підприємство / Відкрите
# акціонерне товариство / Приватне підприємство / Фізична особа-підприємець).
# Russian OOO / ООО (Latin and Cyrillic capitals look identical but are
# different codepoints — both included). Latin LLC / Ltd / Inc / Corp.
_CYR_LEGAL_PREFIXES_RAW = [
    # Verbose Ukrainian forms (full text instead of abbreviations).
    # Empirically 5 of the first 15 prozorro cohort rows use these.
    "ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ",  # = ТОВ
    "ПРИВАТНЕ АКЦІОНЕРНЕ ТОВАРИСТВО",            # = ПрАТ
    "ПУБЛІЧНЕ АКЦІОНЕРНЕ ТОВАРИСТВО",            # = ПАТ
    "ВІДКРИТЕ АКЦІОНЕРНЕ ТОВАРИСТВО",            # = ВАТ
    "ДОЧІРНЄ ПІДПРИЄМСТВО ДЕРЖАВНОЇ КОМПАНІЇ",   # compound: "subsidiary of state company X"
    "ДЕРЖАВНЕ ПІДПРИЄМСТВО",                     # = ДП
    "ДОЧІРНЄ ПІДПРИЄМСТВО",                      # subsidiary
    "ПРИВАТНЕ ПІДПРИЄМСТВО",                     # = ПП
    "ФІЗИЧНА ОСОБА-ПІДПРИЄМЕЦЬ",                 # = ФОП
    # Russian verbose form.
    "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ",  # = OOO / ООО
    # Abbreviations (spec's original list).
    "ТзОВ", "ТОВ", "ПрАТ", "ПАТ", "ДП", "ВАТ", "ПП", "ФОП",
    "OOO", "ООО",                                 # Latin OOO + Cyrillic ООО
    "LLC", "Ltd.", "Ltd", "Inc.", "Inc", "Corp.", "Corp",
]
# Process longest-first so verbose forms are tried before their
# abbreviations (e.g. don't try ТОВ before ТОВАРИСТВО — the
# boundary-char check catches that anyway, but length-sort is faster
# and more obviously correct).
_CYR_LEGAL_PREFIXES = sorted(_CYR_LEGAL_PREFIXES_RAW, key=len, reverse=True)
_CYR_LEGAL_SUFFIXES = ["LLC", "Ltd.", "Ltd", "Inc.", "Inc", "Corp.", "Corp"]
# Surrounding quote characters to strip. Cyrillic guillemets are «»
# (U+00AB / U+00BB); curly Latin quotes (U+201C/D, U+2018/9); plain
# ASCII " '.
_CYR_QUOTES = '«»"“”‘’\'«»'
# Tokens that indicate a beneficial-owner-disclosure paragraph appended
# after a slash — strip everything from the slash onward when one of
# these immediately follows.
_SLASH_DISCLOSURE_RE = re.compile(
    r"/\s*(?:ТОВАРИСТВО|Інформація\s+про|ИНФОРМАЦИЯ\s+О)",
    re.IGNORECASE | re.UNICODE,
)


def _extract_brand_name(raw_name: str | None) -> str | None:
    """Reduce a prozorro/brave1/etc. legal name to a brand-only token
    suitable for `harvestapi/linkedin-company-search`'s `searchQuery`.

    Order of operations:
      1. Trim whitespace.
      2. If the name has a beneficial-owner-disclosure slash trailer
         (``/ ТОВАРИСТВО ...`` or ``/ Інформація про ...``), keep only
         the part before the slash.
      3. Strip legal prefixes (case-insensitive, leading position) and
         legal suffixes (case-insensitive, trailing position) — each
         pass repeats up to 3 times to handle stacked patterns.
      4. Strip surrounding quotes (Cyrillic guillemets + Latin curly +
         ASCII).
      5. Final whitespace trim.
      6. Return None if result is < 3 chars (after strip) or empty.

    Examples per spec:
      'ТОВ "ВІЗАРДЛАБ"' → 'ВІЗАРДЛАБ'
      'ПрАТ «Київстар»' → 'Київстар'
      'ТОВ "Автопоставка-77"/ТОВАРИСТВО З ОБМЕЖЕНОЮ...' → 'Автопоставка-77'
      'WizardLab LLC' → 'WizardLab'
      'TestCo' → 'TestCo'
      '' / '   ' / '«»' → None
    """
    if not raw_name:
        return None
    s = raw_name.strip()
    if not s:
        return None
    # Strip invisible / zero-width characters that some prozorro
    # rows embed inside their legal-form prefix (U+00AD soft hyphen,
    # U+200B/C/D zero-width chars, U+FEFF BOM). Without this,
    # 'ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДА\xadЛЬНІСТЮ' (with a soft
    # hyphen between ВІДПОВІДА and ЛЬНІСТЮ) wouldn't match our
    # prefix list and the whole leg would pass through unstripped.
    s = re.sub(r"[­​‌‍﻿]", "", s)
    # 2. Slash + disclosure trailer
    m = _SLASH_DISCLOSURE_RE.search(s)
    if m:
        s = s[: m.start()].rstrip()
    # 3. Repeat prefix/suffix strip up to 3 times (handles stacked forms
    # like 'ТОВ ПрАТ Foo' which is unlikely but harmless).
    for _ in range(3):
        s_before = s
        s_lower = s.lower()
        for pref in _CYR_LEGAL_PREFIXES:
            if s_lower.startswith(pref.lower()):
                cut = len(pref)
                # Require either end-of-string or whitespace/quote/punct
                # boundary so we don't chew into a legit name that
                # happens to share leading chars (e.g. 'ТОВ' at start
                # of 'ТОВARISTYVO').
                tail = s[cut: cut + 1]
                if tail == "" or not tail.isalnum():
                    s = s[cut:].lstrip(" .,;:-—")
                    s_lower = s.lower()
                    break
        for suf in _CYR_LEGAL_SUFFIXES:
            if s_lower.endswith(suf.lower()):
                cut = len(s) - len(suf)
                head = s[cut - 1: cut] if cut > 0 else ""
                if head == "" or not head.isalnum():
                    s = s[:cut].rstrip(" .,;:-—")
                    s_lower = s.lower()
                    break
        if s == s_before:
            break
    # 4. Strip surrounding quote characters (one layer per side).
    while s and s[0] in _CYR_QUOTES:
        s = s[1:]
    while s and s[-1] in _CYR_QUOTES:
        s = s[:-1]
    s = s.strip()
    # 6. Length floor.
    if len(s) < 3:
        return None
    return s

DEFAULT_CUMULATIVE_BUDGET_CAP_USD = 1.50

log = logging.getLogger("linkedin_url_discovery")


# ── Slug heuristic ──────────────────────────────────────────────────────────
#
# Rules (per session spec):
#  - lowercase
#  - drop legal suffixes (LLC, Inc., Corp., etc.) iteratively from the end
#  - "& Company" / "and Company" / "& Co" / "and Co" → drop the trailing phrase
#  - "&" → "and" (LinkedIn convention, no surrounding spaces inserted —
#                 "AT&T" → "ATandT", "A & B" → "A and B")
#  - strip non-alphanumerics except spaces and hyphens
#  - collapse whitespace + hyphens into single hyphens, lowercase
#  - length check: skip if final slug < 2 chars or > 100 chars

_LEGAL_SUFFIXES = {
    # Always-strip-when-trailing legal/incorp tokens.
    "llc", "lllp", "lp",
    "inc", "incorporated", "corp", "corporation",
    "ltd", "limited",
    "gmbh", "ag", "se", "as", "ab", "oy", "kft",
    "sa", "spa", "srl", "sl", "sas", "bv", "nv",
    "pty", "plc", "company", "co",
    "doo",
}
_TRAILING_PUNCT_RE = re.compile(r"[\s,.\-_/]+$")


def _strip_co_phrase(s: str) -> str:
    """Drop "& Company"/"and Co"/"& Co"/"and Company" trailers."""
    return re.sub(
        r"\s+(?:&|and)\s+(?:company|co\.?)\s*[.,]?\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    )


def _strip_trailing_legal_suffix(s: str) -> str:
    """One pass: if the last whitespace-delimited token is a legal suffix
    (after trimming trailing punctuation), drop it. Caller should call
    repeatedly to peel multiple suffixes."""
    s = _TRAILING_PUNCT_RE.sub("", s)
    parts = s.split()
    if len(parts) <= 1:
        return s
    last = parts[-1].rstrip(".,;:").lower()
    if last in _LEGAL_SUFFIXES:
        return _TRAILING_PUNCT_RE.sub("", " ".join(parts[:-1]))
    return s


def _name_to_slug(name: str | None) -> str | None:
    """Convert a company name to a candidate LinkedIn /company/<slug>.

    Returns None if the name is unusable (empty, all whitespace, all
    punctuation, or strips down to <2 chars after cleaning).
    """
    if not name or not name.strip():
        return None
    s = name.strip()

    # 1. Drop "& Company"/"and Co" trailing phrases first
    s = _strip_co_phrase(s)
    # 2. Replace remaining "&" with "and" (no surrounding spaces inserted —
    #    "AT&T" → "ATandT", "A & B" → "A and B")
    s = s.replace("&", "and")

    # 3. Iteratively peel trailing legal suffixes
    while True:
        new_s = _strip_trailing_legal_suffix(s)
        if new_s == s:
            break
        s = new_s
    s = _TRAILING_PUNCT_RE.sub("", s)

    # 4. Strip everything except ASCII alphanumerics, spaces, and hyphens.
    #    (Names in Cyrillic / Han / Arabic / etc. will reduce to '' here
    #    and fall out via the length check.)
    s = re.sub(r"[^a-zA-Z0-9\s\-]", "", s)

    # 5. Collapse runs of whitespace and hyphens into single hyphens
    s = re.sub(r"[\s\-]+", "-", s.strip())
    s = s.strip("-").lower()

    if len(s) < 2 or len(s) > 100:
        return None
    return s


def _slug_to_url(slug: str) -> str:
    return f"https://www.linkedin.com/company/{slug}"


# Match a LinkedIn /company/<slug> URL and capture the slug. Permissive
# enough to handle both named slugs ("picogrid") and numeric URN slugs
# ("3852270") — Apollo gives us both shapes interchangeably.
_COMPANY_SLUG_RE = re.compile(
    r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([^/?#]+)/?",
    re.IGNORECASE,
)


def _slug_from_linkedin_url(url: str | None) -> str | None:
    """Pull the canonical /company/<slug> portion out of an arbitrary
    LinkedIn URL. Returns None for non-company URLs (e.g. /in/<person>)
    or unrecognisable shapes."""
    if not url:
        return None
    m = _COMPANY_SLUG_RE.match(url.strip())
    if not m:
        return None
    slug = m.group(1).strip().lower()
    if len(slug) < 2 or len(slug) > 100:
        return None
    return slug


def _extract_source_metadata_linkedin(raw_json: str | None) -> str | None:
    """Pull the `linkedin` field out of raw_leads.source_metadata.

    apify_leads.py stores Apollo's `organizationLinkedinUrl` under the
    `linkedin` key inside the source_metadata blob — see _group_by_org
    in that module. Returns None if the JSON is missing/malformed or
    the key isn't present.
    """
    if not raw_json:
        return None
    try:
        meta = json.loads(raw_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    val = meta.get("linkedin") if isinstance(meta, dict) else None
    if not val or not isinstance(val, str):
        return None
    return val.strip() or None


# ── Cohort load + filtering ─────────────────────────────────────────────────


_THIN_PREDICATE = """
    c.website IS NOT NULL AND TRIM(c.website) != ''
    AND (
        (c.description IS NULL OR TRIM(c.description) = '')
        OR (c.hq_country IS NULL OR TRIM(c.hq_country) = '')
        OR c.founded_year IS NULL
        OR (c.employee_count_est IS NULL OR TRIM(c.employee_count_est) = '')
        OR (c.linkedin_url IS NULL OR TRIM(c.linkedin_url) = '')
    )
"""


def load_discovery_cohort(conn: sqlite3.Connection,
                          limit: int | None = None,
                          exclude_sources: list[str] | None = None,
                          include_sources: list[str] | None = None,
                          residual_cohort: bool = False,
                          search_fallback_cohort: bool = False,
                          cyrillic_cohort: bool = False,
                          exclude_attempted_via: str | None = None) -> list[dict]:
    """Load the discovery-eligible cohort.

    Three cohort shapes:
      - Default (`_THIN_PREDICATE`): website-anchored thin companies
        with linkedin_url NULL. The original Phase 2a cohort.
      - `residual_cohort=True` (Phase 2d.1): broader predicate
        anchored on (website OR source_urls) and explicitly excluding
        rejected/portfolio rows. Picks up the 77 rows the website-only
        default misses — 62 of them brave1/prozorro Ukrainian
        companies whose source_urls is populated but website isn't.
      - `search_fallback_cohort=True` (Phase 2d.2): companies whose
        name_heuristic slug-guess produced STALE_SLUG (no LinkedIn
        page at that slug) AND whose linkedin_url is still NULL.
        These are the rows Q13's resolved-Yes search-actor fallback
        targets — companies that exist on LinkedIn under a slug
        shape the name-to-slug heuristic can't reach.

    The `exclude_attempted_via` parameter (Q16 fix / D-020): when
    set to e.g. 'search_actor', the cohort excludes any company that
    has ANY prior `linkedin_url_discoveries` row with that
    `candidate_source`. This is the method-aware exclusion that
    prevents re-attempting the same row with the same method (slug
    attempts don't block search-actor attempts, and vice versa).

    For apify_leads-sourced rows we also pull `raw_leads.source_metadata`
    so we can prefer Apollo's `organizationLinkedinUrl` (stored under the
    `linkedin` key inside that JSON blob — see `_group_by_org` in
    apify_leads.py) over a name-to-slug heuristic. Apollo's URL is more
    accurate per-row but still needs validation, since Apollo's data has
    known quality issues (the Phase 2 bake-off proved this with the
    Odd Systems collision case). The validator pipeline runs unchanged.

    The correlated subquery picks ONE raw_leads.source_metadata per
    company (smallest raw_leads.id) so dup rows can't multiply the cohort.
    """
    where_extra = ""
    params: list = []
    if exclude_sources:
        placeholders = ",".join("?" for _ in exclude_sources)
        where_extra += f" AND (c.source IS NULL OR c.source NOT IN ({placeholders}))"
        params.extend(exclude_sources)
    if include_sources:
        placeholders = ",".join("?" for _ in include_sources)
        where_extra += f" AND c.source IN ({placeholders})"
        params.extend(include_sources)
    if cyrillic_cohort:
        # Phase 2d.3 cohort: prozorro / brave1_articles / NATO DIANA /
        # seed rows that haven't matched yet (linkedin_url NULL, no
        # prior MATCH outcome anywhere in linkedin_url_discoveries).
        # The country filter drops US-tagged rows (this cohort is
        # explicitly non-US-focused). The GLOB '[a-zA-Z0-9]*'
        # alphanumeric requirement is INTENTIONALLY skipped for this
        # branch so Cyrillic-only names ('ТОВ "ВІЗАРДЛАБ"') are
        # accepted.
        cohort_predicate = """
            (c.linkedin_url IS NULL OR TRIM(c.linkedin_url) = '')
            AND (c.status IS NULL OR c.status != 'rejected')
            AND (c.portfolio_company IS NULL OR c.portfolio_company = 0)
            AND c.source IN ('prozorro', 'brave1_articles', 'NATO DIANA 2026', 'seed')
            AND (c.hq_country IS NULL
                 OR c.hq_country NOT IN ('United States', 'USA'))
            AND NOT EXISTS (
                SELECT 1 FROM linkedin_url_discoveries d_match
                WHERE d_match.company_id = c.id
                  AND d_match.outcome = 'MATCH'
            )
        """
    elif search_fallback_cohort:
        # Phase 2d.2 cohort definition (D-020). Companies that
        # already failed the heuristic-slug pass with STALE_SLUG (the
        # actor's "Company not found" response) — these are the rows
        # Q13's resolved-Yes search-actor fallback targets.
        # EXISTS is fine here: ROW-level filter, no JOIN dedup needed
        # because the outer query SELECTs from companies (one row per
        # cohort entry by construction).
        cohort_predicate = """
            (c.linkedin_url IS NULL OR TRIM(c.linkedin_url) = '')
            AND (c.status IS NULL OR c.status != 'rejected')
            AND (c.portfolio_company IS NULL OR c.portfolio_company = 0)
            AND EXISTS (
                SELECT 1 FROM linkedin_url_discoveries d_pre
                WHERE d_pre.company_id = c.id
                  AND d_pre.outcome = 'STALE_SLUG'
                  AND d_pre.candidate_source = 'name_heuristic'
            )
        """
    elif residual_cohort:
        # Phase 2d.1 cohort definition (D-018). Differs from
        # _THIN_PREDICATE in that:
        #   - drops the "missing any firmographic" requirement
        #     (we're chasing URL coverage, not firmographic gaps)
        #   - allows source_urls as an alternative anchor to website
        #   - explicitly excludes rejected + portfolio rows
        cohort_predicate = """
            (c.linkedin_url IS NULL OR TRIM(c.linkedin_url) = '')
            AND (c.status IS NULL OR c.status != 'rejected')
            AND (c.portfolio_company IS NULL OR c.portfolio_company = 0)
            AND (
                (c.website IS NOT NULL AND TRIM(c.website) != '')
                OR (c.source_urls IS NOT NULL AND TRIM(c.source_urls) != '')
            )
        """
    else:
        cohort_predicate = (
            f"{_THIN_PREDICATE}\n"
            f"          AND (c.linkedin_url IS NULL OR TRIM(c.linkedin_url) = '')"
        )

    # Q16 fix (D-020): method-aware exclusion. Excludes companies
    # already attempted with the named candidate_source. Slug-guess
    # attempts don't block search-actor attempts and vice versa.
    if exclude_attempted_via:
        where_extra += (
            " AND c.id NOT IN ("
            "SELECT DISTINCT company_id FROM linkedin_url_discoveries "
            "WHERE candidate_source = ?)"
        )
        params.append(exclude_attempted_via)

    # Cyrillic mode intentionally drops the GLOB '*[a-zA-Z0-9]*'
    # requirement so prozorro / brave1 rows whose names contain only
    # Cyrillic codepoints aren't pre-filtered. All other cohort modes
    # keep the filter to avoid burning actor calls on non-Latin
    # legal-form noise that the slug heuristic can't handle.
    name_alpha_filter = (
        "" if cyrillic_cohort
        else "AND TRIM(c.name) GLOB '*[a-zA-Z0-9]*'"
    )
    sql = f"""
        SELECT c.id, c.name, c.source, c.hq_country, c.website,
               (
                   SELECT rl.source_metadata
                   FROM raw_leads rl
                   JOIN canonical_companies cc ON cc.id = rl.canonical_id
                   WHERE cc.company_id = c.id
                     AND rl.source = 'apify_leads'
                     AND rl.source_metadata IS NOT NULL
                   ORDER BY rl.id ASC
                   LIMIT 1
               ) AS apify_source_metadata
        FROM companies c
        WHERE {cohort_predicate}
          AND c.name IS NOT NULL
          AND LENGTH(TRIM(c.name)) > 2
          AND LENGTH(c.name) <= 280
          {name_alpha_filter}
          AND c.id NOT IN (SELECT DISTINCT company_id FROM linkedin_url_corrections)
          {where_extra}
        ORDER BY c.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    return [
        {"id": r[0], "name": r[1], "source": r[2], "hq_country": r[3],
         "website": r[4], "apify_source_metadata": r[5]}
        for r in rows
    ]


# ── Discovery audit log ─────────────────────────────────────────────────────


def record_discovery(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    attempted_slug: str,
    attempted_url: str,
    outcome: str,
    returned_org_name: str | None = None,
    fuzzy_score: float | None = None,
    candidate_source: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO linkedin_url_discoveries
            (company_id, attempted_slug, attempted_url, outcome,
             returned_org_name, fuzzy_score, candidate_source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (company_id, attempted_slug, attempted_url, outcome,
         returned_org_name, fuzzy_score, candidate_source),
    )


# ── Main pipeline ───────────────────────────────────────────────────────────


@dataclass
class _Candidate:
    company_id: int
    name: str
    source: str | None
    hq_country: str | None
    website: str | None
    slug: str | None
    candidate_url: str | None
    # 'source_metadata' when the slug came from raw_leads.source_metadata.linkedin
    # (Apollo's URL, for apify_leads-sourced rows); 'name_heuristic' for the
    # name-to-slug fallback; None when the row was SKIPped (no usable slug).
    candidate_source: str | None = None


def build_candidates(rows: list[dict]) -> list[_Candidate]:
    """For each row, prefer the LinkedIn URL Apollo already gave us via
    `raw_leads.source_metadata.linkedin` (apify_leads rows only) over the
    name-to-slug heuristic. Either way the candidate URL flows through
    the same validation pipeline downstream, so a wrong-but-real Apollo
    URL still gets caught — never silently written."""
    out: list[_Candidate] = []
    for r in rows:
        slug: str | None = None
        candidate_source: str | None = None
        # Branch 1: trust Apollo's URL if we have it (apify_leads rows).
        apollo_url = _extract_source_metadata_linkedin(
            r.get("apify_source_metadata")
        )
        if apollo_url:
            apollo_slug = _slug_from_linkedin_url(apollo_url)
            if apollo_slug:
                slug = apollo_slug
                candidate_source = "source_metadata"
        # Branch 2: name-to-slug heuristic fallback.
        if not slug:
            heuristic_slug = _name_to_slug(r["name"])
            if heuristic_slug:
                slug = heuristic_slug
                candidate_source = "name_heuristic"
        url = _slug_to_url(slug) if slug else None
        out.append(_Candidate(
            company_id=r["id"], name=r["name"], source=r["source"],
            hq_country=r["hq_country"], website=r["website"],
            slug=slug, candidate_url=url,
            candidate_source=candidate_source,
        ))
    return out


def run_discovery(
    conn: sqlite3.Connection,
    candidates: list[_Candidate],
    *,
    cumulative_cap_usd: float,
    dry_run: bool,
) -> tuple[dict, float]:
    """Build per-candidate URL → validate → write outcomes.
    Returns (outcome_counts, dollars_spent)."""
    counts = {"MATCH": 0, "NEAR_MISS": 0, "COLLISION": 0,
              "STALE_SLUG": 0, "NOT_FOUND": 0, "SKIP": 0,
              "linkedin_url_writes": 0}
    spent = 0.0

    # 1. Skip rows where slug couldn't be generated.
    skips = [c for c in candidates if not c.slug]
    pending = [c for c in candidates if c.slug]
    for c in skips:
        counts["SKIP"] += 1
        if not dry_run:
            record_discovery(
                conn,
                company_id=c.company_id, attempted_slug="",
                attempted_url="", outcome="SKIP",
                candidate_source=c.candidate_source,
            )
    log.info("candidates: %d eligible, %d SKIPped (no slug), %d pending validation",
             len(candidates), len(skips), len(pending))

    # 2. Pre-filter: every URL we generate IS canonical /company/<slug>/ form,
    #    so all pending pass is_valid_company_url. Confirm.
    for c in pending:
        if not is_valid_company_url(c.candidate_url):
            log.warning("generated URL failed pre-filter: %r", c.candidate_url)
            counts["SKIP"] += 1
            if not dry_run:
                record_discovery(
                    conn,
                    company_id=c.company_id, attempted_slug=c.slug or "",
                    attempted_url=c.candidate_url or "", outcome="SKIP",
                    candidate_source=c.candidate_source,
                )
    pending = [c for c in pending if is_valid_company_url(c.candidate_url)]

    # 3. Pre-flight cap check.
    estimated = len(pending) * COST_PER_RESULT_USD
    log.info("pending=%d estimated=$%.4f cap=$%.2f", len(pending), estimated, cumulative_cap_usd)
    if estimated > cumulative_cap_usd:
        raise SystemExit(
            f"BUDGET_ABORT: estimated ${estimated:.4f} > cap ${cumulative_cap_usd:.2f}. "
            f"Re-run with --cumulative-budget-cap to override or --limit to chunk."
        )

    if dry_run:
        # Print sensible-slug preview table and bail.
        print(f"\n{'id':<5} {'source':<18} {'name':<32} → {'candidate_src':<16} {'slug':<30} url")
        print("─" * 150)
        for c in candidates[:50]:
            slug_str = c.slug or "<SKIP>"
            url_str = c.candidate_url or "—"
            cs = c.candidate_source or "<none>"
            print(f"{c.company_id:<5} {(c.source or '')[:18]:<18} "
                  f"{(c.name or '')[:32]:<32} → {cs:<16} {slug_str[:30]:<30} {url_str}")
        return counts, 0.0

    # 4. Batch through harvestapi.
    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        urls  = [c.candidate_url for c in batch]
        items = _call_harvestapi(urls)
        spent += len(items) * COST_PER_RESULT_USD
        idx = _index_results_by_url(items)
        for c in batch:
            it = idx.get((c.candidate_url or "").rstrip("/").lower())
            outcome, returned_name, score = _verdict_from_actor(c, it)
            # D-018: URL-uniqueness guard. Even on MATCH, refuse to write
            # linkedin_url if another company already holds it — downgrade
            # to COLLISION, log, skip the write. Never silently overwrite.
            collision_with: int | None = None
            if outcome == "MATCH":
                row = conn.execute(
                    "SELECT id FROM companies WHERE linkedin_url = ? AND id != ?",
                    (c.candidate_url, c.company_id),
                ).fetchone()
                if row is not None:
                    collision_with = row[0]
                    outcome = "COLLISION"
            counts[outcome] = counts.get(outcome, 0) + 1
            record_discovery(
                conn,
                company_id=c.company_id, attempted_slug=c.slug or "",
                attempted_url=c.candidate_url or "",
                outcome=outcome,
                returned_org_name=returned_name,
                fuzzy_score=score,
                candidate_source=c.candidate_source,
            )
            if outcome == "MATCH":
                conn.execute(
                    "UPDATE companies SET linkedin_url = ? WHERE id = ?",
                    (c.candidate_url, c.company_id),
                )
                counts["linkedin_url_writes"] += 1
            elif collision_with is not None:
                log.warning(
                    "COLLISION: %s already assigned to company %d; skip write for company %d (%s)",
                    c.candidate_url, collision_with, c.company_id, c.name,
                )
        conn.commit()
    return counts, spent


def _hq_country_from_search_item(item: dict) -> str | None:
    """Pull the country code out of the search actor's `locations` array.

    Full mode returns multiple locations per company in arbitrary order
    — first array entry is NOT necessarily the HQ. Pick the entry
    flagged `headquarter=True`; fall back to the first entry; fall back
    to description text via the matcher's helper. Returns None if no
    country resolvable.
    """
    locs = item.get("locations") or []
    if isinstance(locs, list):
        for loc in locs:
            if isinstance(loc, dict) and loc.get("headquarter"):
                country = loc.get("country") or (loc.get("parsed") or {}).get("countryFull")
                if country:
                    return country
        if locs and isinstance(locs[0], dict):
            country = locs[0].get("country") or (locs[0].get("parsed") or {}).get("countryFull")
            if country:
                return country
    return country_from_description(item.get("description"))


def _is_demo_default(items: list[dict]) -> bool:
    """Crunchbase-bake-off lesson: some actors return canned example
    output (e.g. always 'Anduril Industries' regardless of input).
    Detect when EVERY returned item is the same well-known demo seed
    in the actor's exampleRunInput / canned output. This is a coarse
    check — we trip it when the first call against an obscure cohort
    name returns Anduril or other obvious defaults.
    """
    if not items:
        return False
    DEMO_NAMES = {"anduril industries", "openai", "google", "microsoft"}
    names = {(it.get("name") or "").strip().lower() for it in items}
    return len(names) == 1 and names.issubset(DEMO_NAMES)


def search_actor_lookup(
    *, name: str, country: str | None,
    max_items: int = SEARCH_MAX_ITEMS, mode: str = "full",
) -> tuple[list[dict], float]:
    """One-call search against harvestapi/linkedin-company-search.

    Returns (items, spent_usd). `spent_usd` estimated from item count
    using the Full-mode price ($0.001 start + $0.004 per result).
    For Short mode swap COST_PER_RESULT_USD upstream.
    """
    from apify_client import ApifyClient
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise SystemExit("APIFY_TOKEN missing from .env")
    client = ApifyClient(token)
    # Defensive query truncation — the actor enforces a 300-char cap
    # and bad data (4k-char Ukrainian legal-disclosure paragraphs from
    # prozorro) crashed the first live run. Cohort SQL already filters
    # names > 280 chars; this is a belt-and-braces second layer.
    safe_name = (name or "").strip()[:280]
    run_input: dict = {
        "searchQuery": safe_name,
        "maxItems": max_items,
        "scraperMode": mode,
    }
    if country:
        run_input["locations"] = [country]
    t0 = time.time()
    try:
        run = client.actor(SEARCH_ACTOR_ID).call(
            run_input=run_input,
            timeout_secs=600,
        )
    except Exception as e:
        # Any actor-side validation, network, or auth error → skip
        # this row instead of killing the whole batch. The audit log
        # gets a NOT_FOUND row recorded by the caller anyway.
        log.warning("search actor call raised %s for name=%r country=%r; "
                    "treating as 0-item return.", e.__class__.__name__,
                    safe_name, country)
        return [], 0.0
    if not run or run.get("status") != "SUCCEEDED":
        log.warning("search actor run did not succeed: status=%r",
                    (run or {}).get("status"))
        return [], SEARCH_ACTOR_START_COST_USD
    ds_id = run["defaultDatasetId"]
    items = list(client.dataset(ds_id).iterate_items())
    elapsed = time.time() - t0
    per_item = SEARCH_ACTOR_FULL_COST_USD if mode == "full" else 0.002
    spent = SEARCH_ACTOR_START_COST_USD + len(items) * per_item
    log.info("search actor: %r country=%r → %d items in %.1fs (~$%.4f)",
             name, country, len(items), elapsed, spent)
    return items, spent


def _verdict_from_search_results(
    *, our_name: str, our_country: str | None, items: list[dict],
) -> tuple[str, str | None, float | None, str | None]:
    """Classify a search actor's list of candidate items against the
    cohort row. Returns (outcome, returned_name, score, accepted_url).

    Iterates candidates in returned order; accepts the first whose
    canonicalizer verdict is MATCH. If none match: returns the best
    NEAR_MISS / COLLISION verdict encountered (highest score), or
    NOT_FOUND if the actor returned zero items.
    """
    if not items:
        return ("NOT_FOUND", None, None, None)
    best: tuple[str, str | None, float, str | None] = ("NOT_FOUND", None, 0.0, None)
    for it in items:
        url = it.get("linkedinUrl") or ""
        if not is_valid_company_url(url):
            continue
        returned_name = it.get("name") or ""
        returned_country = _hq_country_from_search_item(it)
        cls, score, _reason = classify_match_with_country(
            our_name or "", returned_name, our_country, returned_country,
        )
        if cls == "MATCH":
            return ("MATCH", returned_name, round(score, 1), url)
        # Track the best non-MATCH for fallback verdict.
        if score > best[2]:
            best = (cls, returned_name, score, url)
    if best[0] == "NOT_FOUND":
        # No URLs even passed the company-URL prefilter.
        return ("NOT_FOUND", None, None, None)
    return (best[0], best[1], round(best[2], 1), best[3])


def _cyrillic_query_for_row(row: dict) -> tuple[str | None, str | None]:
    """Resolve (searchQuery, location_filter) for a Cyrillic-mode row.

    - searchQuery comes from `_extract_brand_name(row.name)`. None → skip.
    - location_filter: 'Ukraine' for prozorro rows (override); otherwise
      the row's `hq_country` if set, else None (no filter).
    """
    brand = _extract_brand_name(row.get("name"))
    if not brand:
        return None, None
    if row.get("source") == "prozorro":
        return brand, "Ukraine"
    return brand, (row.get("hq_country") or None)


def run_search_fallback(
    conn: sqlite3.Connection,
    cohort: list[dict],
    *,
    cumulative_cap_usd: float,
    dry_run: bool,
    mode: str,
    limit: int | None = None,
    cyrillic_mode: bool = False,
) -> tuple[dict, float, str]:
    """Per-company search-actor invocation. Returns (counts, spent, halt_reason).

    halt_reason is one of: "completed", "budget_cap", "zero_match_window".

    `cyrillic_mode=True` (Phase 2d.3) re-uses the same per-row loop but
    swaps the actor query for `_extract_brand_name(row.name)` (strips
    Ukrainian/Russian legal prefixes + surrounding quotes + slash-
    appended beneficial-owner disclosures) and forces `locations =
    ['Ukraine']` for prozorro-sourced rows.
    """
    counts = {"MATCH": 0, "NEAR_MISS": 0, "COLLISION": 0,
              "NOT_FOUND": 0, "STALE_SLUG": 0, "SKIP": 0,
              "linkedin_url_writes": 0, "d018_collisions": 0,
              "attempts": 0, "brand_extraction_skipped": 0}
    spent = 0.0
    per_co_est = SEARCH_ACTOR_START_COST_USD + SEARCH_MAX_ITEMS * (
        SEARCH_ACTOR_FULL_COST_USD if mode == "full" else 0.002
    )
    estimated = len(cohort) * per_co_est
    log.info("search-fallback preflight: cohort=%d per_co_est=$%.4f "
             "estimated=$%.4f cap=$%.2f mode=%s max_items=%d "
             "cyrillic_mode=%s",
             len(cohort), per_co_est, estimated, cumulative_cap_usd,
             mode, SEARCH_MAX_ITEMS, cyrillic_mode)
    if estimated > cumulative_cap_usd:
        log.warning("estimated $%.4f > cap $%.2f — will graceful-stop "
                    "mid-cohort.", estimated, cumulative_cap_usd)
    if dry_run:
        print(f"\n{'id':<6} {'source':<18} {'name':<40} → search_actor candidate query")
        print("─" * 130)
        for c in cohort[:50]:
            if cyrillic_mode:
                q, loc = _cyrillic_query_for_row(c)
                q_disp = q if q is not None else "<SKIP — empty after extraction>"
                print(f"{c['id']:<6} {(c['source'] or '')[:18]:<18} "
                      f"{(c['name'] or '')[:40]:<40} → "
                      f"q={q_disp!r}  loc={loc!r}")
            else:
                print(f"{c['id']:<6} {(c['source'] or '')[:18]:<18} "
                      f"{(c['name'] or '')[:40]:<40} → "
                      f"q={c['name']!r}  country={c['hq_country']!r}")
        return counts, 0.0, "dry_run"

    halt_reason = "completed"
    n_to_process = len(cohort) if limit is None else min(limit, len(cohort))
    for i, c in enumerate(cohort[:n_to_process]):
        # Predictive cap check
        if spent + per_co_est > cumulative_cap_usd:
            log.warning("GRACEFUL STOP at row %d / %d (spent $%.4f, "
                        "next would exceed cap $%.2f).",
                        i, n_to_process, spent, cumulative_cap_usd)
            halt_reason = "budget_cap"
            break

        if cyrillic_mode:
            query, location = _cyrillic_query_for_row(c)
            if not query:
                # Brand extraction produced no usable token (Cyrillic
                # quotes-only, < 3 chars, etc.). Record SKIP and move
                # on — no actor spend.
                counts["brand_extraction_skipped"] += 1
                counts["SKIP"] += 1
                record_discovery(
                    conn,
                    company_id=c["id"], attempted_slug="",
                    attempted_url="", outcome="SKIP",
                    returned_org_name=None, fuzzy_score=None,
                    candidate_source="search_actor",
                )
                continue
        else:
            query = c["name"]
            location = c.get("hq_country")
        items, item_spent = search_actor_lookup(
            name=query, country=location,
            max_items=SEARCH_MAX_ITEMS, mode=mode,
        )
        spent += item_spent
        counts["attempts"] += 1
        # The our_name fed to the canonicalizer below is the same
        # token used as the actor query: in cyrillic mode that's the
        # extracted brand (gives the matcher a fair shot at scoring
        # against a Cyrillic-formatted returned name); in default
        # mode it's the raw company name as before.
        canonicalizer_our_name = query

        # Demo-default guard — abort if early call returns canned output.
        if i < 3 and _is_demo_default(items):
            raise SystemExit(
                f"ABORT: search actor returned demo defaults on "
                f"attempt {i+1} (items names: "
                f"{[it.get('name') for it in items]!r}). "
                f"Refusing to spend further; investigate actor input "
                f"shape before re-running."
            )

        outcome, returned_name, score, accepted_url = _verdict_from_search_results(
            our_name=canonicalizer_our_name, our_country=c.get("hq_country"),
            items=items,
        )

        # Log every candidate the actor returned, not just the
        # accepted/best one — full audit trail per spec step 3.
        if items:
            for it in items:
                url = it.get("linkedinUrl") or ""
                if not is_valid_company_url(url):
                    continue
                ret_name = it.get("name") or ""
                ret_country = _hq_country_from_search_item(it)
                it_cls, it_score, _ = classify_match_with_country(
                    canonicalizer_our_name or "", ret_name,
                    c.get("hq_country"), ret_country,
                )
                slug_match = _COMPANY_SLUG_RE.match(url)
                slug = slug_match.group(1) if slug_match else ""
                record_discovery(
                    conn,
                    company_id=c["id"], attempted_slug=slug,
                    attempted_url=url,
                    outcome=it_cls,
                    returned_org_name=ret_name,
                    fuzzy_score=round(it_score, 1),
                    candidate_source="search_actor",
                )
        else:
            # Zero items returned — record one NOT_FOUND audit row.
            record_discovery(
                conn,
                company_id=c["id"], attempted_slug="",
                attempted_url="", outcome="NOT_FOUND",
                returned_org_name=None, fuzzy_score=None,
                candidate_source="search_actor",
            )

        # D-018 uniqueness guard before writing the accepted URL.
        if outcome == "MATCH" and accepted_url:
            existing = conn.execute(
                "SELECT id FROM companies WHERE linkedin_url = ? AND id != ?",
                (accepted_url, c["id"]),
            ).fetchone()
            if existing is not None:
                counts["d018_collisions"] += 1
                log.warning(
                    "D-018 COLLISION: %s already assigned to company %d; "
                    "skip search-fallback write for company %d (%s)",
                    accepted_url, int(existing[0]), c["id"], c["name"],
                )
                outcome = "COLLISION"
            else:
                conn.execute(
                    "UPDATE companies SET linkedin_url = ? WHERE id = ?",
                    (accepted_url, c["id"]),
                )
                counts["linkedin_url_writes"] += 1
        counts[outcome] = counts.get(outcome, 0) + 1

        # Commit periodically so a mid-batch crash doesn't lose work.
        if (i + 1) % 10 == 0:
            conn.commit()

        # Halt-on-0-MATCH window per spec.
        if (i + 1) == SEARCH_HALT_WINDOW and counts["MATCH"] == 0:
            log.error(
                "ZERO_MATCH HALT: %d attempts produced 0 MATCH (well "
                "below any plausible signal floor). Stopping; spent "
                "$%.4f. Inspect linkedin_url_discoveries rows with "
                "candidate_source='search_actor' for diagnosis.",
                SEARCH_HALT_WINDOW, spent,
            )
            halt_reason = "zero_match_window"
            break
    conn.commit()
    return counts, spent, halt_reason


def _verdict_from_actor(c: _Candidate, it: dict | None) -> tuple[str, str | None, float | None]:
    """Translate one actor result into (outcome, returned_name, score).

    Outcomes:
      MATCH / NEAR_MISS / COLLISION  — name match path (with country check)
      STALE_SLUG — actor returned 'Company not found' or our URL didn't come back
      NOT_FOUND  — same as STALE_SLUG, kept distinct in the schema for telemetry
                   (currently reuses STALE_SLUG since harvestapi conflates them)
    """
    if it is None:
        return ("STALE_SLUG", None, None)
    err = it.get("error") or it.get("errorDescription") or ""
    if err and "not found" in err.lower():
        return ("STALE_SLUG", None, None)
    returned_name = it.get("name")
    if not returned_name:
        return ("STALE_SLUG", None, None)
    returned_country = (it.get("locations") or [{}])[0].get("country") \
        or country_from_description(it.get("description"))
    cls, score, _reason = classify_match_with_country(
        c.name or "", returned_name, c.hq_country, returned_country,
    )
    return (cls, returned_name, round(score, 1))


# ── CLI / self-test ─────────────────────────────────────────────────────────


_SLUG_TESTS: list[tuple[str, str | None]] = [
    # (input, expected slug; None means SKIP)
    ("Picogrid",                                  "picogrid"),
    ("ATA Engineering",                           "ata-engineering"),
    ("Atlas Space Operations",                    "atlas-space-operations"),
    ("BlueBird Aero Systems Ltd.",                "bluebird-aero-systems"),
    ("Acme Corp, Inc.",                           "acme"),
    ("AT&T Defense",                              "atandt-defense"),
    ("3M Company",                                "3m"),
    ("",                                          None),
    ("  ",                                        None),
    ("A & B",                                     "a-and-b"),
    # extra edge cases:
    ("ENGINEERING AND SOFTWARE SYSTEM SOLUTIONS, INC.",
                                                   "engineering-and-software-system-solutions"),
    ('ТОВ "ВІЗАРДЛАБ"',                           None),     # all-Cyrillic → strip → empty
    ("REMOTE HEALTH SOLUTIONS LLC",               "remote-health-solutions"),
    ("FGC Plasma Solutions, Inc.",                "fgc-plasma-solutions"),
    ("McQ Inc.",                                  "mcq"),
    ("DittoLive Incorporated",                    "dittolive"),
    ("Critical Frequency Design, LLC",            "critical-frequency-design"),
]


def _self_test() -> int:
    failures: list[str] = []
    for given, expected in _SLUG_TESTS:
        got = _name_to_slug(given)
        if got == expected:
            print(f"  PASS  in={given!r:<55} → out={got!r}")
        else:
            msg = f"  FAIL  in={given!r:<55} → got={got!r}, expected={expected!r}"
            print(msg); failures.append(msg)
    print(f"\n{len(_SLUG_TESTS) - len(failures)}/{len(_SLUG_TESTS)} slug-heuristic cases passed")

    # --- Cohort SQL filter test (read-only against the live DB) ---
    print("\n--- cohort SQL filter ---")
    try:
        conn = sqlite3.connect(DB_PATH)
        n_all      = len(load_discovery_cohort(conn))
        n_excl_sbir = len(load_discovery_cohort(conn, exclude_sources=["SBIR"]))
        # Per-source counts via the same load fn — single-source exclusion
        # should remove exactly that source's rows.
        sources_present = sorted({r["source"] for r in load_discovery_cohort(conn)})
        n_sbir_only = n_all - n_excl_sbir
        # Expectation: n_excl_sbir > 0, n_excl_sbir < n_all, and n_excl_sbir +
        # (rows where source==SBIR) == n_all.
        ok = n_excl_sbir > 0 and n_excl_sbir < n_all
        n_sbir_direct = sum(1 for r in load_discovery_cohort(conn) if r["source"] == "SBIR")
        ok = ok and (n_sbir_direct == n_sbir_only)
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  cohort_all={n_all}  cohort_excl_sbir={n_excl_sbir}  "
              f"diff={n_sbir_only}  direct_count_of_SBIR={n_sbir_direct}")
        print(f"        sources in unfiltered cohort: {sources_present}")
        if not ok:
            failures.append("cohort SQL filter test")
        conn.close()
    except Exception as e:
        failures.append(f"cohort SQL filter raised: {e!r}")
        print(f"  FAIL  cohort SQL filter raised: {e!r}")

    # --- source_metadata candidate-selection tests (pure-Python) ---
    print("\n--- source_metadata candidate selection ---")
    candidate_cases: list[tuple[str, dict, str, str | None]] = [
        # (label, row dict, expected candidate_source, expected slug)
        (
            "apify_leads row with source_metadata.linkedin → source_metadata wins",
            {"id": 1, "name": "Robin Radar Systems", "source": "apify_leads",
             "hq_country": "Netherlands", "website": "https://robinradar.com",
             "apify_source_metadata": json.dumps(
                 {"linkedin": "https://www.linkedin.com/company/3852270"}
             )},
            "source_metadata",
            "3852270",
        ),
        (
            "apify_leads row with source_metadata but no 'linkedin' key → name fallback",
            {"id": 2, "name": "Picogrid", "source": "apify_leads",
             "hq_country": "United States", "website": "https://picogrid.com",
             "apify_source_metadata": json.dumps({"industry": "Defense"})},
            "name_heuristic",
            "picogrid",
        ),
        (
            "non-apify_leads row → name heuristic always wins (no source_metadata pull)",
            {"id": 3, "name": "ATA Engineering, Inc.", "source": "SBIR",
             "hq_country": "United States", "website": "https://ata-e.com",
             "apify_source_metadata": None},
            "name_heuristic",
            "ata-engineering",
        ),
        (
            "apify_leads row, source_metadata.linkedin is /in/<person> → unparseable → name fallback",
            {"id": 4, "name": "TestCo Robotics", "source": "apify_leads",
             "hq_country": "Germany", "website": "https://testco.example",
             "apify_source_metadata": json.dumps(
                 {"linkedin": "https://www.linkedin.com/in/some-person"}
             )},
            "name_heuristic",
            "testco-robotics",
        ),
        (
            "apify_leads row with malformed source_metadata JSON → name fallback",
            {"id": 5, "name": "Atlas Space Operations", "source": "apify_leads",
             "hq_country": "United States", "website": "https://atlas.example",
             "apify_source_metadata": "{not valid json"},
            "name_heuristic",
            "atlas-space-operations",
        ),
    ]
    built = build_candidates([r for _, r, _, _ in candidate_cases])
    for (label, _row, want_cs, want_slug), got in zip(candidate_cases, built):
        ok = got.candidate_source == want_cs and got.slug == want_slug
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  {label}")
        print(f"        got candidate_source={got.candidate_source!r} slug={got.slug!r}  "
              f"expected={want_cs!r}, {want_slug!r}")
        if not ok:
            failures.append(label)

    # --- Phase 2d.3 _extract_brand_name (Cyrillic-mode tokenizer) ---
    print("\n--- _extract_brand_name (Cyrillic legal-prefix stripping) ---")
    brand_cases: list[tuple[str, str | None, str | None]] = [
        ("ТОВ \"ВІЗАРДЛАБ\"", "ВІЗАРДЛАБ",
         "Ukrainian ТОВ prefix + ASCII quotes around brand"),
        (
            "ТОВ \"Автопоставка-77\"/ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ \"АВТОПОСТАВКА-77\" "
            "(ТОВ \"АВТОПОСТАВКА-77\") Інформація про кінцевого",
            "Автопоставка-77",
            "ТОВ prefix + ASCII quotes + slash-disclosure trailer"),
        ("ПрАТ «Київстар»", "Київстар",
         "ПрАТ prefix + Cyrillic guillemets"),
        ("WizardLab LLC", "WizardLab",
         "Latin LLC suffix"),
        ("TestCo", "TestCo",
         "Latin name with no prefix or suffix — unchanged"),
        ("", None, "Empty string → None"),
        ("   ", None, "Pure whitespace → None"),
        ("«»", None, "Just quotes → None (under 3-char floor after strip)"),
        ("ПАТ «Нафтогаз України»", "Нафтогаз України",
         "ПАТ prefix + Cyrillic guillemets, multi-word brand"),
        ("ДП \"Антонов\"", "Антонов",
         "ДП (Державне підприємство) + quotes"),
        ("ФОП Іваненко І.І.", "Іваненко І.І.",
         "ФОП prefix (sole proprietor) — keep name"),
    ]
    for inp, expected, label in brand_cases:
        got = _extract_brand_name(inp)
        ok = got == expected
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  {label!s}")
        print(f"        in={inp!r}")
        print(f"        got={got!r}  expected={expected!r}")
        if not ok:
            failures.append(label)

    # --- Phase 2d.2 search-fallback helpers ---
    print("\n--- _hq_country_from_search_item (Full mode response unwrap) ---")
    hq_cases: list[tuple[str, dict, str | None]] = [
        (
            "headquarter=True picks HQ over first non-HQ entry "
            "(Anduril: locations[0]=London/GB, locations[3]=HQ/US)",
            {"locations": [
                {"country": "GB", "city": "London", "headquarter": False},
                {"country": "US", "city": "Atlanta", "headquarter": False},
                {"country": "US", "city": "Orange", "headquarter": True},
            ]},
            "US",
        ),
        (
            "no headquarter flag → locations[0].country",
            {"locations": [
                {"country": "DE", "city": "Berlin", "headquarter": False},
                {"country": "FR", "city": "Paris", "headquarter": False},
            ]},
            "DE",
        ),
        (
            "empty locations → description fallback finds country name",
            {"locations": [], "description": "Headquartered in the United States. Builds drones."},
            "US",
        ),
        (
            "no signal at all → None",
            {"locations": [], "description": "We build cool things."},
            None,
        ),
        (
            "headquarter via locations[].parsed.countryFull",
            {"locations": [
                {"parsed": {"countryFull": "Ukraine"}, "headquarter": True},
            ]},
            "Ukraine",
        ),
    ]
    for label, item, expected in hq_cases:
        got = _hq_country_from_search_item(item)
        ok = got == expected
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        print(f"        got={got!r}  expected={expected!r}")
        if not ok:
            failures.append(label)

    # --- _verdict_from_search_results: classify the actor's items ---
    print("\n--- _verdict_from_search_results (search actor verdict) ---")
    verdict_cases: list[tuple[str, dict, list[dict], str, str | None]] = [
        (
            "first item MATCH at score 100 → MATCH wins",
            {"our_name": "Anduril Industries", "our_country": "United States"},
            [{"name": "Anduril Industries",
              "linkedinUrl": "https://www.linkedin.com/company/anduril/",
              "locations": [{"country": "US", "headquarter": True}],
              "description": ""}],
            "MATCH", "https://www.linkedin.com/company/anduril/",
        ),
        (
            "first item country-mismatch → NEAR_MISS (best non-MATCH)",
            {"our_name": "Anduril", "our_country": "United States"},
            [{"name": "Anduril",
              "linkedinUrl": "https://www.linkedin.com/company/anduril-xyz/",
              "locations": [{"country": "DE", "headquarter": True}],
              "description": ""}],
            "NEAR_MISS", "https://www.linkedin.com/company/anduril-xyz/",
        ),
        (
            "zero items → NOT_FOUND, accepted_url None",
            {"our_name": "Obscure Co", "our_country": "United States"},
            [],
            "NOT_FOUND", None,
        ),
        (
            "second item MATCHes → MATCH taken (first was a low-score fail)",
            {"our_name": "Picogrid", "our_country": "United States"},
            [
                {"name": "Pico Holdings", "linkedinUrl": "https://www.linkedin.com/company/pico-hold/",
                 "locations": [{"country": "US", "headquarter": True}], "description": ""},
                {"name": "Picogrid", "linkedinUrl": "https://www.linkedin.com/company/picogrid/",
                 "locations": [{"country": "US", "headquarter": True}], "description": ""},
            ],
            "MATCH", "https://www.linkedin.com/company/picogrid/",
        ),
        (
            "item with /in/ URL filtered by company-URL prefilter → falls to NOT_FOUND",
            {"our_name": "TestCo", "our_country": "United States"},
            [{"name": "Not A Company", "linkedinUrl": "https://www.linkedin.com/in/some-person",
              "locations": [{"country": "US", "headquarter": True}]}],
            "NOT_FOUND", None,
        ),
    ]
    for label, args_, items, want_cls, want_url in verdict_cases:
        cls, _rname, _sc, url = _verdict_from_search_results(
            our_name=args_["our_name"], our_country=args_["our_country"],
            items=items,
        )
        ok = cls == want_cls and url == want_url
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        print(f"        got cls={cls!r} url={url!r}  expected={want_cls!r}, {want_url!r}")
        if not ok:
            failures.append(label)

    # --- _is_demo_default canned-output catcher ---
    print("\n--- _is_demo_default (canned-output guard) ---")
    demo_cases = [
        ("empty list is not demo", [], False),
        ("single Anduril item triggers demo flag",
         [{"name": "Anduril Industries"}], True),
        ("mixed names are not demo",
         [{"name": "Anduril Industries"}, {"name": "Picogrid"}], False),
        ("legit obscure company is not demo",
         [{"name": "FGC Plasma Solutions"}], False),
    ]
    for label, items, expected in demo_cases:
        got = _is_demo_default(items)
        ok = got == expected
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  got={got} expected={expected}")
        if not ok:
            failures.append(label)

    # --- Q16 method-aware cohort exclusion test (read-only on live DB) ---
    print("\n--- cohort_load: exclude_attempted_via ---")
    try:
        conn = sqlite3.connect(DB_PATH)
        # Baseline (no exclusion) — should give the search-fallback cohort size
        n_no_excl = len(load_discovery_cohort(conn, search_fallback_cohort=True))
        # With exclusion, on first run search_actor count is 0 → same cohort
        n_with_excl = len(load_discovery_cohort(
            conn, search_fallback_cohort=True,
            exclude_attempted_via="search_actor",
        ))
        # Pre-existing attempts via name_heuristic counted (sanity)
        n_excl_name = len(load_discovery_cohort(
            conn, search_fallback_cohort=True,
            exclude_attempted_via="name_heuristic",
        ))
        ok = n_no_excl == n_with_excl  # search_actor hasn't run yet
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  search_fallback cohort:        {n_no_excl}")
        print(f"        excluding via 'search_actor':    {n_with_excl}")
        print(f"        excluding via 'name_heuristic':  {n_excl_name}  "
              f"(should be 0 — every row HAS a name_heuristic attempt by definition)")
        if not ok:
            failures.append("Q16 exclusion test")
        conn.close()
    except Exception as e:
        failures.append(f"Q16 exclusion test raised: {e!r}")
        print(f"  FAIL  Q16 exclusion test raised: {e!r}")

    return 0 if not failures else 1


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 2a — heuristic LinkedIn-URL discovery for thin companies.",
    )
    p.add_argument("--limit", type=int, default=None,
                   help="Cap at N companies for batch tests (Phases B/C).")
    p.add_argument("--cumulative-budget-cap", type=float,
                   default=DEFAULT_CUMULATIVE_BUDGET_CAP_USD,
                   help=f"Override cumulative-spend cap (default "
                        f"${DEFAULT_CUMULATIVE_BUDGET_CAP_USD:.2f}).")
    p.add_argument("--exclude-source", action="append", default=[], metavar="SOURCE",
                   dest="exclude_sources",
                   help="Skip companies where companies.source == SOURCE. "
                        "Repeatable. Used by Phase D to chunk by source priority "
                        "(e.g. `--exclude-source SBIR` for the non-SBIR pass).")
    p.add_argument("--include-source", action="append", default=[], metavar="SOURCE",
                   dest="include_sources",
                   help="Restrict to companies where companies.source == SOURCE. "
                        "Repeatable. Used for source-scoped runs "
                        "(e.g. `--include-source SBIR` for SBIR-only).")
    p.add_argument("--residual-cohort", action="store_true",
                   dest="residual_cohort",
                   help="Phase 2d.1 cohort: broader predicate anchored on "
                        "(website OR source_urls) for any non-rejected, "
                        "non-portfolio row with linkedin_url NULL. Picks up "
                        "the rows the default website-only thin predicate "
                        "misses (incl. brave1/prozorro Ukrainian companies).")
    p.add_argument("--search-fallback", action="store_true",
                   dest="search_fallback",
                   help="Phase 2d.2 (D-020): use harvestapi/linkedin-"
                        "company-search keyword-search actor against the "
                        "STALE_SLUG cohort (name_heuristic rows whose "
                        "slug-guess didn't exist on LinkedIn). Q16 cohort "
                        "exclusion applies — companies already attempted "
                        "with candidate_source='search_actor' are excluded.")
    p.add_argument("--cyrillic-mode", action="store_true",
                   dest="cyrillic_mode",
                   help="Phase 2d.3: target prozorro / brave1_articles "
                        "/ NATO DIANA / seed rows whose names carry "
                        "Ukrainian or Russian legal-form wrappers (ТОВ, "
                        "ПрАТ, etc.). Pre-extracts the brand via "
                        "_extract_brand_name() before sending to the "
                        "search actor. Forces locations=['Ukraine'] for "
                        "prozorro rows. Requires --search-fallback.")
    p.add_argument("--search-mode", choices=("short", "full"), default="full",
                   help="Search actor scraper mode. Default 'full' "
                        "(D-014 needs structured country, which Short "
                        "mode does not return). 'short' available for "
                        "cost-only exploratory runs.")
    p.add_argument("--dry-run", action="store_true",
                   help="Generate slugs and print preview; no actor calls, no DB writes.")
    p.add_argument("--self-test", action="store_true",
                   help="Run inline _name_to_slug test cases + cohort SQL filter test; "
                        "no I/O for the slug tests, read-only SQL for the cohort test.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if args.self_test:
        return _self_test()
    _setup_logging()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Concurrent writers (Phase 3 historically; Codex scrapers under
    # the SCRAPER work today) may hold the write lock for non-trivial
    # bursts. The first Phase 2d.2 production run crashed at attempt
    # ~878 with `database is locked` despite the 30s timeout below;
    # bumped to 180s as a defensive measure. The pragma is per-
    # connection so this doesn't widen any other collector's window.
    conn.execute("PRAGMA busy_timeout = 180000")

    # Phase 2d.2/2d.3 search-fallback flow takes its own cohort + run loop.
    if args.search_fallback:
        if args.cyrillic_mode:
            rows = load_discovery_cohort(
                conn, limit=args.limit,
                exclude_sources=args.exclude_sources or None,
                include_sources=args.include_sources or None,
                cyrillic_cohort=True,
                exclude_attempted_via="search_actor",
            )
        else:
            rows = load_discovery_cohort(
                conn, limit=args.limit,
                exclude_sources=args.exclude_sources or None,
                include_sources=args.include_sources or None,
                search_fallback_cohort=True,
                exclude_attempted_via="search_actor",
            )
        # Q16 verification logging — count of companies excluded
        # specifically because they were already attempted via
        # search_actor. Should be 0 on the first Phase 2d.2 run.
        excluded_count = conn.execute(
            "SELECT COUNT(DISTINCT company_id) FROM linkedin_url_discoveries "
            "WHERE candidate_source = 'search_actor'"
        ).fetchone()[0]
        log.info("search-fallback cohort: %d row(s) (limit=%s, "
                 "Q16-excluded-by-search_actor=%d, mode=%s, cyrillic_mode=%s)",
                 len(rows), args.limit, excluded_count, args.search_mode,
                 args.cyrillic_mode)
        if (not args.cyrillic_mode
                and not (2000 <= len(rows) * 2 <= 2400)
                and excluded_count == 0):
            # The spec's "2,000-2,400" range is in cumulative
            # STALE_SLUG discovery rows; the distinct-company count
            # is ~half that. Cyrillic mode is a different (smaller)
            # cohort shape so the sanity check doesn't apply.
            row_count_proxy = sum(
                1 for _ in conn.execute(
                    "SELECT 1 FROM linkedin_url_discoveries d "
                    "WHERE d.outcome='STALE_SLUG' "
                    "AND d.candidate_source='name_heuristic' "
                    "AND EXISTS (SELECT 1 FROM companies c WHERE c.id = d.company_id "
                    "  AND (c.linkedin_url IS NULL OR TRIM(c.linkedin_url) = ''))"
                )
            )
            if not (2000 <= row_count_proxy <= 2400):
                log.warning(
                    "Cohort row count %d is outside the spec's "
                    "2,000-2,400 expected range. Proceeding (distinct-co "
                    "cohort=%d), but verify the predicate matches "
                    "expectations.", row_count_proxy, len(rows),
                )

        counts, spent, halt_reason = run_search_fallback(
            conn, rows,
            cumulative_cap_usd=args.cumulative_budget_cap,
            dry_run=args.dry_run, mode=args.search_mode,
            limit=args.limit,
            cyrillic_mode=args.cyrillic_mode,
        )

        if args.dry_run:
            log.info("(dry-run: nothing written to DB)")
            return 0
        print("\n=== search-fallback summary ===")
        for k in ("MATCH", "NEAR_MISS", "COLLISION", "NOT_FOUND", "SKIP"):
            print(f"  {k:<11} {counts.get(k, 0)}")
        print(f"  attempts            : {counts.get('attempts', 0)}")
        print(f"  linkedin_url writes : {counts.get('linkedin_url_writes', 0)}")
        print(f"  D-018 collisions    : {counts.get('d018_collisions', 0)}")
        print(f"  halt reason         : {halt_reason}")
        print(f"  spend               : ${spent:.4f}")
        # D-021: reconcile portfolio_company markers against the
        # portfolio table + thesis YAML, after writes commit. New
        # rows / updated websites may newly match a portfolio entry.
        try:
            from ..ingest import mark_portfolio_companies  # noqa: E402
            n_marked = mark_portfolio_companies(conn)
            if n_marked:
                log.info("portfolio reconciliation: %d newly-marked row(s)", n_marked)
        except Exception as e:
            log.warning("portfolio reconciliation skipped: %s", e)
        return 0 if halt_reason != "zero_match_window" else 2

    # Default heuristic-slug discovery path (Phase 2a / Phase 2d.1).
    rows = load_discovery_cohort(conn, limit=args.limit,
                                 exclude_sources=args.exclude_sources or None,
                                 include_sources=args.include_sources or None,
                                 residual_cohort=args.residual_cohort)
    candidates = build_candidates(rows)
    log.info("loaded %d discovery-eligible row(s) (limit=%s exclude_sources=%s "
             "include_sources=%s residual_cohort=%s)",
             len(candidates), args.limit,
             args.exclude_sources or "[]",
             args.include_sources or "[]",
             args.residual_cohort)

    counts, spent = run_discovery(
        conn, candidates,
        cumulative_cap_usd=args.cumulative_budget_cap,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        log.info("(dry-run: nothing written to DB)")
        return 0

    print("\n=== discovery summary ===")
    for k in ("MATCH", "NEAR_MISS", "COLLISION", "STALE_SLUG", "NOT_FOUND", "SKIP"):
        print(f"  {k:<11} {counts.get(k, 0)}")
    print(f"  linkedin_url writes : {counts.get('linkedin_url_writes', 0)}")
    print(f"  spend               : ${spent:.4f}")
    # D-021: portfolio reconciliation after every discovery run.
    try:
        from ..ingest import mark_portfolio_companies  # noqa: E402
        n_marked = mark_portfolio_companies(conn)
        if n_marked:
            log.info("portfolio reconciliation: %d newly-marked row(s)", n_marked)
    except Exception as e:
        log.warning("portfolio reconciliation skipped: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
