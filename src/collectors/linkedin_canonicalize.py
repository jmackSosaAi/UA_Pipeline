"""LinkedIn-URL canonicalisation pass — validate `companies.linkedin_url`.

The Phase-2 bake-off (commit 6b17ee8) found that 4 of 5 sampled
linkedin_url values pointed at the wrong company. Three were stale
slugs (LinkedIn 404), one was a slug collision (a different real
company at the same `/company/<slug>`). The bake-off actors faithfully
returned data for whichever company actually exists at the slug — with
no signal that it's not ours. If we ran Phase-2 firmographics
enrichment as-is, we'd silently overwrite our companies' fields with
the wrong companies' fields.

This module is the prerequisite gate. For each non-null
`companies.linkedin_url`, it:

  1. Pre-filters URLs that are obviously wrong (e.g. `/in/<slug>`
     personal profiles instead of `/company/<slug>`) — STALE_SLUG, no
     actor call needed.
  2. Calls harvestapi/linkedin-company in batches and inspects the
     returned organisation name.
  3. Fuzzy-matches our `companies.name` against `returned.name` using a
     stdlib token-set ratio:
       MATCH      score ≥ 80  → leave linkedin_url alone
       NEAR_MISS  60 ≤ <80    → log to corrections table, leave URL alone
                                (operator decides via correct_linkedin_url.py)
       COLLISION  score < 60  → nullify linkedin_url, record old value
       STALE_SLUG actor error → nullify linkedin_url, record old value
       NOT_FOUND  same as STALE_SLUG (harvestapi error string matches both)

Modes:
  --diagnostic-only        : report cohort size + estimated cost; no calls
  --dry-run-cohort phase2-bakeoff
                           : run on the bake-off 5; print results;
                             NO DB writes, NO nullifications
  --validate-all           : full cohort; write corrections; nullify
                             linkedin_url for COLLISION + STALE_SLUG

A $1.00 cumulative budget cap applies (override with
--cumulative-budget-cap). Pre-flight estimate vs. cap aborts before
any actor call when projected spend would breach.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Resolve project root + load env explicitly (script may be invoked outside
# the project working directory).
_HERE = Path(__file__).resolve()
ROOT = _HERE.parent.parent.parent
load_dotenv(dotenv_path=ROOT / ".env")

from .base import DB_PATH                         # noqa: E402
from .dedup import normalize_name                  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────────

ACTOR_ID = "harvestapi/linkedin-company"
COST_PER_RESULT_USD = 0.004
DEFAULT_CUMULATIVE_BUDGET_CAP_USD = 1.00
BATCH_SIZE = 25                                    # harvestapi handles batches well

MATCH_THRESHOLD     = 80.0
NEAR_MISS_THRESHOLD = 60.0

PHASE2_BAKEOFF_IDS = (749, 1146, 1154, 298, 306)

log = logging.getLogger("linkedin_canonicalize")


# ── Match logic ──────────────────────────────────────────────────────────────

_TOKEN_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def _tokenize(s: str) -> list[str]:
    """Lowercase, normalise legal suffixes, split on non-word chars."""
    if not s:
        return []
    cleaned = normalize_name(s)
    return [t for t in _TOKEN_SPLIT.split(cleaned.lower()) if t]


def token_set_ratio(s1: str, s2: str) -> float:
    """Token-set fuzzy-match ratio in [0, 100].

    Mirrors rapidfuzz.fuzz.token_set_ratio semantics using stdlib difflib
    so we don't add a new dependency. The trick: build three strings — the
    sorted token intersection alone, that intersection + sorted-A-only
    diffs, and that intersection + sorted-B-only diffs — and return the
    max pairwise SequenceMatcher ratio across them.
    """
    a, b = set(_tokenize(s1)), set(_tokenize(s2))
    if not a or not b:
        return 0.0
    intersection = a & b
    diff_ab = a - b
    diff_ba = b - a
    sorted_int = " ".join(sorted(intersection))
    sorted_a   = (sorted_int + " " + " ".join(sorted(diff_ab))).strip()
    sorted_b   = (sorted_int + " " + " ".join(sorted(diff_ba))).strip()
    if not sorted_int:
        # No shared tokens; SequenceMatcher between sorted_a and sorted_b
        # is the only signal. (When intersection is empty, sorted_int is
        # also "" and the other ratios degenerate.)
        return difflib.SequenceMatcher(None, sorted_a, sorted_b).ratio() * 100.0
    pairs = (
        difflib.SequenceMatcher(None, sorted_int, sorted_a).ratio(),
        difflib.SequenceMatcher(None, sorted_int, sorted_b).ratio(),
        difflib.SequenceMatcher(None, sorted_a,   sorted_b).ratio(),
    )
    return max(pairs) * 100.0


def classify_match(our_name: str, returned_name: str) -> tuple[str, float]:
    """Return ('MATCH'|'NEAR_MISS'|'COLLISION', score) on NAME ALONE.

    See `classify_match_with_country` for the production path that adds
    a HQ-country cross-check on top of this — needed because two
    different real companies can share a trade name (the bake-off
    'Odd Systems' case: Ukrainian defense-tech vs. South African
    industrial firm both at name 'ODD SYSTEMS').
    """
    score = token_set_ratio(our_name, returned_name)
    if score >= MATCH_THRESHOLD:
        return ("MATCH", score)
    if score >= NEAR_MISS_THRESHOLD:
        return ("NEAR_MISS", score)
    return ("COLLISION", score)


# ── Country normalisation (light) ────────────────────────────────────────────
#
# harvestapi returns ISO-2 country codes (e.g. "US", "ZA"); our companies
# table mostly stores English names ("United States", "Ukraine"). To
# cross-check, we map both sides through this small dict and compare
# normalised codes. Coverage is intentionally limited to the country
# universe we expect in our pipeline (NATO + Ukraine + close allies +
# common collision destinations); unmapped values fall through and the
# country check is skipped (treated as "unknown — don't downgrade").

_COUNTRY_NAME_TO_ISO = {
    "united states": "US", "usa": "US", "us": "US", "united states of america": "US",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB",
    "ukraine": "UA",
    "germany": "DE",
    "france": "FR",
    "italy": "IT",
    "spain": "ES",
    "netherlands": "NL", "the netherlands": "NL",
    "belgium": "BE", "luxembourg": "LU", "portugal": "PT",
    "denmark": "DK", "norway": "NO", "iceland": "IS",
    "greece": "GR", "turkey": "TR", "türkiye": "TR",
    "poland": "PL",
    "czech republic": "CZ", "czechia": "CZ",
    "slovakia": "SK", "hungary": "HU", "romania": "RO", "bulgaria": "BG",
    "slovenia": "SI", "croatia": "HR", "albania": "AL",
    "estonia": "EE", "latvia": "LV", "lithuania": "LT",
    "montenegro": "ME", "north macedonia": "MK", "canada": "CA",
    "finland": "FI", "sweden": "SE",
    "australia": "AU", "new zealand": "NZ", "japan": "JP",
    "south korea": "KR", "korea, republic of": "KR",
    "singapore": "SG", "israel": "IL",
    "switzerland": "CH", "ireland": "IE", "austria": "AT",
    "south africa": "ZA",
    "india": "IN",
    "russia": "RU", "russian federation": "RU",
}


def normalise_country(value: str | None) -> str | None:
    """Best-effort lower→ISO2 mapping. Returns None if unmappable."""
    if not value:
        return None
    s = value.strip().lower()
    if not s:
        return None
    # Already an ISO-2 code?
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return _COUNTRY_NAME_TO_ISO.get(s)


def country_from_description(description: str | None) -> str | None:
    """Description-based country fallback when `locations[0].country` is null.

    harvestapi's structured country field is non-deterministic — a row that
    returns `country='ZA'` in one call returns `country=null` in the next.
    The description text is stable. Looks for the longest known country
    name as a substring (longer first, so 'South Africa' matches before
    'Africa' would). Returns ISO-2 or None.
    """
    if not description:
        return None
    text = description.lower()
    # Match longer names first to avoid 'Africa' inside 'South Africa', etc.
    for name in sorted(_COUNTRY_NAME_TO_ISO.keys(), key=len, reverse=True):
        # Word-boundary substring (allow trailing punctuation)
        if name in text and (
            text == name
            or f" {name}" in text
            or text.startswith(name)
            or any(p in text for p in (f"{name}.", f"{name},", f"{name}\n"))
        ):
            return _COUNTRY_NAME_TO_ISO[name]
    return None


def classify_match_with_country(
    our_name: str, returned_name: str,
    our_country: str | None, returned_country: str | None,
) -> tuple[str, float, str | None]:
    """Name-based classification + HQ-country cross-check.

    Returns (classification, name_score, downgrade_reason). Logic:
      - cls != MATCH: pass through (NEAR_MISS / COLLISION / etc.)
      - cls == MATCH AND both countries known AND disagree:
          downgrade to NEAR_MISS with explicit mismatch reason
      - cls == MATCH AND our_country is NULL/empty (D-014, v2):
          downgrade to NEAR_MISS — we can't cross-check the name
          match, so we defer to operator review rather than write a
          potentially-wrong linkedin_url. The Anima collision
          (Phase 2c id 283) is the canonical case: our DB had
          hq_country NULL, "Anima" name-matched a Greek industrial
          firm at /company/anima with country='GR', and v1's
          permissive bypass let the wrong URL through.
      - cls == MATCH AND our_country known AND returned_country
        NULL/empty: still MATCH. harvestapi's locations[0].country
        non-determinism (D-008) means the SCRAPER side is unreliable;
        we're not going to penalise rows for that. The description-
        text fallback in country_from_description() is the layer
        that handles harvestapi NULLs upstream.
      - Otherwise (both known and agree): MATCH.
    Reason is non-None only when a downgrade happened.
    """
    cls, score = classify_match(our_name, returned_name)
    if cls != "MATCH":
        return (cls, score, None)
    a = normalise_country(our_country)
    b = normalise_country(returned_country)
    if not a:
        # D-014: our DB has no country to cross-check against.
        # Defer to manual review rather than silently bypass.
        return ("NEAR_MISS", score,
                "country_check_skipped_due_to_null_db_country")
    if b and a != b:
        return ("NEAR_MISS", score,
                f"name MATCH (score={score:.0f}) but country mismatch: ours={a!r} theirs={b!r}")
    return ("MATCH", score, None)


# ── URL pre-filter ───────────────────────────────────────────────────────────

_VALID_COMPANY_URL_RE = re.compile(
    r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[^/?#]+/?$",
    re.IGNORECASE,
)


def is_valid_company_url(url: str | None) -> bool:
    return bool(url) and bool(_VALID_COMPANY_URL_RE.match(url.strip()))


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass
class CohortRow:
    company_id: int
    name: str
    linkedin_url: str
    source: str | None
    hq_country: str | None = None


@dataclass
class Verdict:
    company_id: int
    our_name: str
    linkedin_url: str
    classification: str         # MATCH | NEAR_MISS | COLLISION | STALE_SLUG
    fuzzy_score: float | None   # None for STALE_SLUG
    returned_name: str | None   # None for STALE_SLUG
    downgrade_reason: str | None = None  # set when country cross-check downgraded MATCH→NEAR_MISS
    raw_actor_payload: dict = field(default_factory=dict)


# ── DB ───────────────────────────────────────────────────────────────────────


def load_cohort(conn: sqlite3.Connection, ids: list[int] | None = None) -> list[CohortRow]:
    if ids:
        placeholders = ",".join("?" for _ in ids)
        sql = (
            f"SELECT id, name, linkedin_url, source, hq_country FROM companies "
            f"WHERE id IN ({placeholders})"
        )
        rows = conn.execute(sql, ids).fetchall()
    else:
        sql = (
            "SELECT id, name, linkedin_url, source, hq_country FROM companies "
            "WHERE linkedin_url IS NOT NULL AND TRIM(linkedin_url) != ''"
        )
        rows = conn.execute(sql).fetchall()
    return [
        CohortRow(company_id=r[0], name=r[1] or "", linkedin_url=(r[2] or "").strip(),
                  source=r[3], hq_country=r[4])
        for r in rows
        if r[2] and (r[2] or "").strip()
    ]


def record_correction(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    old_url: str,
    returned_name: str | None,
    correction_type: str,
    fuzzy_score: float | None,
) -> None:
    conn.execute(
        """
        INSERT INTO linkedin_url_corrections
            (company_id, old_linkedin_url, returned_org_name,
             correction_type, fuzzy_score)
        VALUES (?, ?, ?, ?, ?)
        """,
        (company_id, old_url, returned_name, correction_type, fuzzy_score),
    )


def nullify_linkedin_url(conn: sqlite3.Connection, company_id: int) -> None:
    conn.execute(
        "UPDATE companies SET linkedin_url = NULL WHERE id = ?",
        (company_id,),
    )


# ── Actor call ───────────────────────────────────────────────────────────────


def _call_harvestapi(urls: list[str]) -> list[dict]:
    """Single batched call. Returns dataset items in input order (or with
    error rows for not-found URLs)."""
    from apify_client import ApifyClient
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise SystemExit("APIFY_TOKEN missing from .env")
    client = ApifyClient(token)
    log.info("calling %s with %d URL(s)", ACTOR_ID, len(urls))
    t0 = time.time()
    run = client.actor(ACTOR_ID).call(run_input={"companies": urls},
                                       timeout_secs=600)
    if not run or run.get("status") != "SUCCEEDED":
        log.error("actor run failed: status=%r", (run or {}).get("status"))
        return []
    ds_id = run["defaultDatasetId"]
    items = list(client.dataset(ds_id).iterate_items())
    log.info("returned %d items in %.1fs (run=%s)",
             len(items), time.time() - t0, run.get("id"))
    return items


def _index_results_by_url(items: list[dict]) -> dict[str, dict]:
    """Map LinkedIn URL → result dict.

    harvestapi can return:
      - normal result with `linkedinUrl` set
      - error row with linkedinUrl=None and error string
    For the error case we must fish the URL out of `error` text or
    `originalQuery.search`.
    """
    out: dict[str, dict] = {}
    for it in items:
        url = it.get("linkedinUrl") or ""
        if not url:
            err = (it.get("error") or "")
            # error format: 'Company not found: `<url>`'
            m = re.search(r"`([^`]+)`", err)
            if m:
                url = m.group(1)
            else:
                url = (it.get("originalQuery") or {}).get("search") or ""
        url = url.rstrip("/").lower()
        if url:
            out[url] = it
    return out


def _slug_match(a: str, b: str) -> bool:
    """Allow trailing-slash differences and case variation."""
    return a.rstrip("/").lower() == b.rstrip("/").lower()


# ── Pipeline ─────────────────────────────────────────────────────────────────


def evaluate_cohort(
    cohort: list[CohortRow],
    *,
    cumulative_cap_usd: float,
    skip_actor_call: bool = False,
) -> tuple[list[Verdict], float]:
    """Run pre-filter + actor pass; return verdicts and dollars spent.
    skip_actor_call=True is for diagnostic-only mode."""
    verdicts: list[Verdict] = []
    pending: list[CohortRow] = []
    for row in cohort:
        if not is_valid_company_url(row.linkedin_url):
            verdicts.append(Verdict(
                company_id=row.company_id, our_name=row.name,
                linkedin_url=row.linkedin_url,
                classification="STALE_SLUG", fuzzy_score=None,
                returned_name=None,
                raw_actor_payload={"reason": "url-not-/company/-form"},
            ))
        else:
            pending.append(row)

    estimated_spend = len(pending) * COST_PER_RESULT_USD
    log.info(
        "pre-filter: %d cohort rows, %d pre-flagged STALE_SLUG (bad URL form), "
        "%d pending actor call (estimated $%.4f)",
        len(cohort), len(verdicts), len(pending), estimated_spend,
    )
    if estimated_spend > cumulative_cap_usd:
        raise SystemExit(
            f"BUDGET_ABORT: estimated ${estimated_spend:.4f} > cap "
            f"${cumulative_cap_usd:.2f}. Override with --cumulative-budget-cap."
        )
    if skip_actor_call:
        return verdicts, 0.0

    spent_usd = 0.0
    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        urls  = [b.linkedin_url for b in batch]
        items = _call_harvestapi(urls)
        spent_usd += len(items) * COST_PER_RESULT_USD
        idx = _index_results_by_url(items)
        for row in batch:
            it = idx.get(row.linkedin_url.rstrip("/").lower())
            if it is None:
                # Not in returned results — unusual; treat as STALE_SLUG with empty payload
                verdicts.append(Verdict(
                    company_id=row.company_id, our_name=row.name,
                    linkedin_url=row.linkedin_url,
                    classification="STALE_SLUG", fuzzy_score=None,
                    returned_name=None,
                    raw_actor_payload={"reason": "url-not-in-batch-response"},
                ))
                continue
            err = it.get("error") or it.get("errorDescription") or ""
            returned_name = it.get("name")
            if err and "not found" in err.lower():
                verdicts.append(Verdict(
                    company_id=row.company_id, our_name=row.name,
                    linkedin_url=row.linkedin_url,
                    classification="STALE_SLUG", fuzzy_score=None,
                    returned_name=None, raw_actor_payload={"error": err},
                ))
                continue
            if not returned_name:
                # Shouldn't happen for a successful row; log + skip
                log.warning("row %d has no name and no error: %s",
                            row.company_id, str(it)[:200])
                verdicts.append(Verdict(
                    company_id=row.company_id, our_name=row.name,
                    linkedin_url=row.linkedin_url,
                    classification="STALE_SLUG", fuzzy_score=None,
                    returned_name=None,
                    raw_actor_payload={"reason": "no-name-and-no-error"},
                ))
                continue
            returned_country = (it.get("locations") or [{}])[0].get("country")
            if not returned_country:
                # Fallback: harvestapi's structured country is non-deterministic;
                # description text is stable.
                returned_country = country_from_description(it.get("description"))
            cls, score, reason = classify_match_with_country(
                row.name, returned_name,
                row.hq_country, returned_country,
            )
            verdicts.append(Verdict(
                company_id=row.company_id, our_name=row.name,
                linkedin_url=row.linkedin_url,
                classification=cls, fuzzy_score=round(score, 1),
                returned_name=returned_name,
                downgrade_reason=reason,
                raw_actor_payload={"name": returned_name,
                                   "country": returned_country,
                                   "description": (it.get("description") or "")[:160]},
            ))
    return verdicts, spent_usd


def apply_verdicts(conn: sqlite3.Connection, verdicts: list[Verdict],
                   *, dry_run: bool) -> dict:
    """Write corrections to linkedin_url_corrections + nullify URLs for
    COLLISION + STALE_SLUG. NEAR_MISS is logged to corrections but URL is
    LEFT IN PLACE — operator decides via correct_linkedin_url.py.

    Returns counts dict."""
    counts = {"MATCH": 0, "NEAR_MISS": 0, "COLLISION": 0, "STALE_SLUG": 0,
              "nullified": 0}
    for v in verdicts:
        counts[v.classification] = counts.get(v.classification, 0) + 1
        if v.classification == "MATCH":
            continue
        if dry_run:
            continue
        record_correction(
            conn,
            company_id=v.company_id, old_url=v.linkedin_url,
            returned_name=v.returned_name,
            correction_type=v.classification,
            fuzzy_score=v.fuzzy_score,
        )
        if v.classification in ("COLLISION", "STALE_SLUG"):
            nullify_linkedin_url(conn, v.company_id)
            counts["nullified"] += 1
    if not dry_run:
        conn.commit()
    return counts


# ── CLI ──────────────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="LinkedIn URL canonicalisation pass for companies.linkedin_url.",
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--diagnostic-only", action="store_true",
                     help="Print cohort + estimated cost. No actor calls.")
    grp.add_argument("--dry-run-cohort", choices=["phase2-bakeoff"],
                     help="Run validator on a fixed-id cohort. NO DB writes.")
    grp.add_argument("--validate-all", action="store_true",
                     help="Full cohort. Write corrections + nullify bad URLs.")
    grp.add_argument("--self-test", action="store_true",
                     help="Run inline matcher tests; no I/O.")
    p.add_argument("--cumulative-budget-cap", type=float,
                   default=DEFAULT_CUMULATIVE_BUDGET_CAP_USD,
                   help=f"Override cumulative spend cap (default "
                        f"${DEFAULT_CUMULATIVE_BUDGET_CAP_USD:.2f}).")
    return p.parse_args()


def _self_test() -> int:
    """Inline matcher test cases. No DB or actor."""
    cases: list[tuple[str, str, str, str]] = [
        # (label, our_name, returned_name, expected classification)
        ("identical",                   "Picogrid",                  "Picogrid",            "MATCH"),
        ("legal-suffix",                "ATA Engineering, Inc.",     "ATA Engineering",     "MATCH"),
        ("punctuation-and-case",        "Odd Systems",               "ODD SYSTEMS",         "MATCH"),
        ("rebrand or wrong",            "ANONYMOUS A.I INC",         "DeepMedia AI",        "COLLISION"),
        ("subset name (looks like ours)", "Edge Aerospace",          "Edge Group",          "NEAR_MISS"),
        ("totally different",           "Picogrid",                  "YouTube",             "COLLISION"),
        ("Cyrillic / Latin mismatch",   'ТОВ "ВІЗАРДЛАБ"',           "WizardLab UA",        "COLLISION"),
        ("trailing common token",       "DittoLive Incorporated",    "DittoLive",           "MATCH"),
    ]
    failures = []
    for label, ours, theirs, expected in cases:
        got, score = classify_match(ours, theirs)
        ok = got == expected
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  {label:<35} our={ours!r:<30} their={theirs!r:<25} "
              f"score={score:.1f}  expect={expected:<10} got={got}")
        if not ok:
            failures.append(label)
    print(f"\n{len(cases) - len(failures)}/{len(cases)} name-only matcher cases passed")

    # ── D-014 / canonicalize v2: NULL-country branch in classify_match_with_country ──
    print("\n--- classify_match_with_country (D-014: NULL-country branch) ---")
    NULL_REASON = "country_check_skipped_due_to_null_db_country"
    cwc_cases: list[tuple[str, str, str, str | None, str | None, str, str | None]] = [
        # (label, our_name, returned_name, our_country, returned_country,
        #  expected_class, expected_reason_substring_or_None)
        ("D-014 NEW: our_country=None + name MATCH → NEAR_MISS w/ explicit reason",
            "Anima", "Anima", None, "GR",
            "NEAR_MISS", NULL_REASON),
        ("D-014 NEW: our_country='' (empty) + name MATCH → NEAR_MISS w/ same reason",
            "Anima", "Anima", "", "UA",
            "NEAR_MISS", NULL_REASON),
        ("D-014 NEW: our_country='   ' (whitespace) + name MATCH → NEAR_MISS w/ same reason",
            "Anima", "Anima", "   ", "US",
            "NEAR_MISS", NULL_REASON),
        ("regression: known countries that disagree → NEAR_MISS w/ MISMATCH reason (not D-014's)",
            "Odd Systems", "ODD SYSTEMS", "Ukraine", "ZA",
            "NEAR_MISS", "country mismatch"),
        ("regression: known countries that agree → MATCH (no downgrade)",
            "Picogrid", "Picogrid", "United States", "US",
            "MATCH", None),
        ("D-008 preserved: our_country known but theirs NULL → still MATCH",
            "Picogrid", "Picogrid", "United States", None,
            "MATCH", None),
        ("non-MATCH name (NEAR_MISS) + NULL country → unchanged NEAR_MISS",
            "Edge Aerospace", "Edge Group", None, "US",
            "NEAR_MISS", None),
        ("COLLISION pass-through unaffected by country branches",
            "Picogrid", "YouTube", None, None,
            "COLLISION", None),
    ]
    for label, ours, theirs, oc, tc, exp_cls, exp_reason in cwc_cases:
        got_cls, got_score, got_reason = classify_match_with_country(
            ours, theirs, oc, tc,
        )
        cls_ok = got_cls == exp_cls
        if exp_reason is None:
            reason_ok = got_reason is None
        else:
            reason_ok = got_reason is not None and exp_reason in got_reason
        ok = cls_ok and reason_ok
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  {label}")
        if not ok:
            print(f"        got cls={got_cls!r} reason={got_reason!r}")
            print(f"        expected cls={exp_cls!r} reason~={exp_reason!r}")
            failures.append(label)

    n_total = len(cases) + len(cwc_cases)
    print(f"\n{n_total - len(failures)}/{n_total} matcher + cross-check cases passed")
    return 0 if not failures else 1


def main() -> int:
    args = _parse_args()
    if args.self_test:
        return _self_test()

    _setup_logging()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.diagnostic_only:
        cohort = load_cohort(conn)
        n_bad_form = sum(1 for r in cohort if not is_valid_company_url(r.linkedin_url))
        n_actor    = len(cohort) - n_bad_form
        print(f"\n=== diagnostic-only ===")
        print(f"  cohort size              : {len(cohort)}")
        print(f"  pre-flagged bad URL form : {n_bad_form}")
        print(f"  pending actor call       : {n_actor}")
        print(f"  estimated spend          : ${n_actor * COST_PER_RESULT_USD:.4f}")
        print(f"  budget cap               : ${args.cumulative_budget_cap:.2f}")
        return 0

    if args.dry_run_cohort == "phase2-bakeoff":
        cohort = load_cohort(conn, ids=list(PHASE2_BAKEOFF_IDS))
        print(f"\n=== dry-run on phase2-bakeoff cohort ({len(cohort)} rows) ===")
        verdicts, spent = evaluate_cohort(cohort,
                                          cumulative_cap_usd=args.cumulative_budget_cap)
        counts = apply_verdicts(conn, verdicts, dry_run=True)
        print(f"\n  spend: ${spent:.4f}")
        print(f"  verdicts:")
        for v in verdicts:
            score_str = f"{v.fuzzy_score:.1f}" if v.fuzzy_score is not None else " - "
            their = v.returned_name or "(none)"
            reason = f"  [{v.downgrade_reason}]" if v.downgrade_reason else ""
            print(f"    id={v.company_id:<5} {v.classification:<10} "
                  f"score={score_str:<5}  ours={v.our_name!r:<35} their={their!r}{reason}")
        print(f"\n  counts: {counts}")
        # Sanity gate: 4/5 expected wrong
        expected_wrong = 4
        actual_wrong = sum(counts.get(c, 0) for c in ("STALE_SLUG", "COLLISION", "NEAR_MISS"))
        if actual_wrong < 3:  # 3 stale slugs are dead-certain; allow ±1 on collision
            print(f"\n  WARNING: only {actual_wrong}/5 flagged wrong "
                  "(expected ≥3 from bake-off). Matcher may be too lenient.")
            return 2
        print(f"\n  ✓ {actual_wrong}/5 flagged wrong (expected ≥3) — matcher OK")
        return 0

    if args.validate_all:
        cohort = load_cohort(conn)
        print(f"\n=== validate-all on full cohort ({len(cohort)} rows) ===")
        verdicts, spent = evaluate_cohort(cohort,
                                          cumulative_cap_usd=args.cumulative_budget_cap)
        counts = apply_verdicts(conn, verdicts, dry_run=False)
        print(f"\n=== summary ===")
        print(f"  total validated  : {len(verdicts)}")
        print(f"  MATCH            : {counts.get('MATCH', 0)}")
        print(f"  NEAR_MISS        : {counts.get('NEAR_MISS', 0)} (left in place — manual review)")
        print(f"  COLLISION        : {counts.get('COLLISION', 0)} (linkedin_url nullified)")
        print(f"  STALE_SLUG       : {counts.get('STALE_SLUG', 0)} (linkedin_url nullified)")
        print(f"  total nullified  : {counts.get('nullified', 0)}")
        print(f"  spend            : ${spent:.4f}")
        # Persist verdicts for the review report
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
        verdicts_path = ROOT / "data" / f"linkedin_url_verdicts_{stamp}.json"
        verdicts_path.parent.mkdir(parents=True, exist_ok=True)
        verdicts_path.write_text(json.dumps([v.__dict__ for v in verdicts], indent=2,
                                            default=str))
        print(f"  verdicts saved to: {verdicts_path}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
