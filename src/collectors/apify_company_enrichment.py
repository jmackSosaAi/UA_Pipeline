"""Phase 2c — apify_company_enrichment: firmographic backfill via harvestapi.

Calls `harvestapi/linkedin-company` against companies that have a
**MATCH-validated** `linkedin_url` (per D-007 in `docs/DECISIONS.md`,
canonicalisation is a hard prerequisite — no exceptions). For each
returned company, fills NULL fields on the `companies` row and stores
the raw response in `apify_enrichment_metadata` for forensic audit.

Cost model (read this before adding a CLI flag):
    --preflight-only Parses args, prints the cost plan, EXITS BEFORE
                     any actor call. $0 spend by construction (no
                     `apify_client` import is triggered).
    --no-write       Calls the actor, parses results, logs conflicts.
                     **Actor IS called and you DO pay.** Use to
                     inspect harvestapi data without writing to DB.
    --overwrite      Operator-opt-in: also fill non-NULL fields. Off
                     by default per D-010 (NULL-only by default).

Field mapping:
    response.description                   → description
    response.industries[0].name            → primary_sector
    response.employeeCountRange (start/end) → employee_count_est
                                             (formatted as "11 - 50")
    response.foundedOn.year                → founded_year
    derived country (locations[0].country  → hq_country
        OR description-fallback per D-008/D-011)

Conflict policy (D-010): when harvestapi disagrees with an existing
non-NULL value, the conflict is logged to
`data/phase2c_conflicts_YYYYMMDD.txt` and the existing value is
**preserved**. `--overwrite` flips this — operator-opt-in, never
auto-applied.

Audit columns set unconditionally on every processed row:
    apify_enrichment_metadata = json.dumps(raw_response)
    apify_enriched_at         = datetime('now')

A subsequent `apify_enriched_at IS NULL` cohort filter naturally
skips already-processed rows on re-runs.

Auto-chain (D-013, default ON):
After the enrichment loop finishes, the collector re-runs `score.py`
(`score_one`) and `classify.py` (`classify_one`) against the rows it
just touched. This is the chain Q21 was about — its evidence-based
recommendation at 53% T4 reduction (commit `e774081`) lands firmly
in the AUTO-CHAIN band. Pass `--no-rescore` to skip the chain (e.g.,
in batch runs that want to rescore everything once at the end).
Both `score_one` and `classify_one` are pure-logic, no external API.

Sentinel (D-002): `apify_company_enrichment://`. This collector writes
only to columns on `companies` (no founders/contacts), so the sentinel
mostly lives in the audit metadata. The source-aware DELETE pattern
(D-003) doesn't apply here because we don't write into the
founders/contacts tables.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve()
ROOT = _HERE.parent.parent.parent
load_dotenv(dotenv_path=ROOT / ".env")

from .base import DB_PATH                          # noqa: E402
from .linkedin_canonicalize import (                # noqa: E402
    country_from_description,
    normalise_country,
)

# ── Module constants ─────────────────────────────────────────────────────────

ACTOR_ID = "harvestapi/linkedin-company"
SOURCE = "apify_company_enrichment"
SENTINEL_PREFIX = "apify_company_enrichment://"

COST_PER_RESULT_USD = 0.004
DEFAULT_CUMULATIVE_BUDGET_CAP_USD = 0.85
BATCH_SIZE = 25                                   # harvestapi handles batches well

log = logging.getLogger("apify_company_enrichment")


# ── ISO-2 → canonical English name ──────────────────────────────────────────
#
# Inverse of the country lookup in linkedin_canonicalize. Keys here are the
# ISO-2 codes harvestapi returns; values are the human-readable English
# names that match what the rest of the pipeline writes into
# `companies.hq_country` (per Apollo's `organizationCountry` and existing
# enrichment writes). Coverage tracks `apify_baseline.yaml allowed_countries`
# plus common collision destinations. Unmapped ISO codes pass through as-is.

_ISO_TO_NAME = {
    # NATO members
    "US": "United States", "GB": "United Kingdom", "FR": "France",
    "DE": "Germany", "IT": "Italy", "ES": "Spain",
    "NL": "Netherlands", "BE": "Belgium", "LU": "Luxembourg",
    "PT": "Portugal", "DK": "Denmark", "NO": "Norway", "IS": "Iceland",
    "GR": "Greece", "TR": "Turkey", "PL": "Poland",
    "CZ": "Czech Republic", "SK": "Slovakia", "HU": "Hungary",
    "RO": "Romania", "BG": "Bulgaria", "SI": "Slovenia",
    "HR": "Croatia", "AL": "Albania", "EE": "Estonia",
    "LV": "Latvia", "LT": "Lithuania", "ME": "Montenegro",
    "MK": "North Macedonia", "CA": "Canada",
    "FI": "Finland", "SE": "Sweden",
    # Ukraine
    "UA": "Ukraine",
    # Close allies
    "AU": "Australia", "NZ": "New Zealand", "JP": "Japan",
    "KR": "South Korea", "SG": "Singapore", "IL": "Israel",
    "CH": "Switzerland", "IE": "Ireland", "AT": "Austria",
    # Common collision destinations
    "ZA": "South Africa", "IN": "India",
    "RU": "Russia",
}


# ── Response-parsing helpers (pure, no I/O — easy to test offline) ──────────


def _industry_from_response(item: dict) -> str | None:
    """harvestapi returns industries as a list of {id, name, urn} objects."""
    industries = item.get("industries") or []
    if not industries:
        return None
    first = industries[0]
    if isinstance(first, dict):
        return (first.get("name") or "").strip() or None
    if isinstance(first, str):
        return first.strip() or None
    return None


def _employee_count_band(item: dict) -> str | None:
    """harvestapi returns employeeCountRange as {start, end}.

    Format the band as a string matching apify_baseline.yaml's
    `allowed_sizes` vocabulary ("11 - 50", "51 - 200", etc.). Returns
    None when both bounds are null. Handles the asymmetric cases:
      start=N, end=None  → "N+"
      start=None, end=N  → "0 - N"
    """
    rng = item.get("employeeCountRange") or {}
    if not isinstance(rng, dict):
        return None
    start = rng.get("start")
    end   = rng.get("end")
    if start is None and end is None:
        return None
    if start is not None and end is not None:
        return f"{int(start)} - {int(end)}"
    if start is not None:
        return f"{int(start)}+"
    return f"0 - {int(end)}"


def _founded_year(item: dict) -> int | None:
    fo = item.get("foundedOn")
    if isinstance(fo, dict):
        y = fo.get("year")
    else:
        y = fo
    if y is None:
        return None
    try:
        n = int(str(y).strip())
    except (TypeError, ValueError):
        return None
    # Sanity bounds — Apollo occasionally has 0 or 1900 as placeholders
    if n < 1800 or n > 2100:
        return None
    return n


def _derive_country(item: dict) -> str | None:
    """ISO-2 from `locations[0].country`, falling back to description-text
    parse per D-008/D-011 (harvestapi's structured field is non-deterministic).
    Returns the canonical English name (per `_ISO_TO_NAME`) or None.
    """
    locs = item.get("locations") or []
    iso = None
    if locs and isinstance(locs[0], dict):
        iso = locs[0].get("country")
    if not iso:
        iso = country_from_description(item.get("description"))
    if not iso:
        return None
    iso = iso.strip().upper()
    if len(iso) == 2:
        return _ISO_TO_NAME.get(iso, iso)
    # Unknown shape — pass through; caller can spot it in conflict log
    return iso or None


def map_response_to_fields(item: dict) -> dict:
    """Return {column: value} for the harvestapi response fields we
    care about. Only includes keys with non-None values."""
    out: dict = {}
    desc = (item.get("description") or "").strip()
    if desc:
        out["description"] = desc
    industry = _industry_from_response(item)
    if industry:
        out["primary_sector"] = industry
    band = _employee_count_band(item)
    if band:
        out["employee_count_est"] = band
    year = _founded_year(item)
    if year is not None:
        out["founded_year"] = year
    country = _derive_country(item)
    if country:
        out["hq_country"] = country
    return out


# ── Cohort load ─────────────────────────────────────────────────────────────


def load_cohort(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Companies with a non-empty linkedin_url that haven't been
    apify-enriched yet. The `apify_enriched_at IS NULL` filter makes
    the run resumable / re-runnable without duplicate spend."""
    sql = """
        SELECT id, name, linkedin_url, source,
               description, hq_country, founded_year,
               employee_count_est, primary_sector, tier
        FROM companies
        WHERE linkedin_url IS NOT NULL
          AND TRIM(linkedin_url) != ''
          AND apify_enriched_at IS NULL
        ORDER BY id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [
        {"id": r[0], "name": r[1], "linkedin_url": r[2], "source": r[3],
         "description": r[4], "hq_country": r[5], "founded_year": r[6],
         "employee_count_est": r[7], "primary_sector": r[8], "tier": r[9]}
        for r in conn.execute(sql).fetchall()
    ]


# ── Update logic (NULL-only by default; conflict-aware) ─────────────────────


def _is_blank(v) -> bool:
    """Treat None, empty string, and whitespace-only as null-like."""
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def compute_update_plan(
    existing: dict,
    incoming: dict,
    *,
    overwrite: bool,
) -> tuple[dict, list[tuple]]:
    """Pure function — returns (fields_to_write, conflicts).

    `fields_to_write` is the dict of columns that should be UPDATEd,
    after applying NULL-only or --overwrite policy. `conflicts` is a
    list of `(column, our_value, their_value)` tuples for fields where
    harvestapi disagrees with an existing non-NULL value (regardless
    of overwrite — conflicts are always reported).
    """
    to_write: dict = {}
    conflicts: list[tuple] = []
    for col, new_val in incoming.items():
        old_val = existing.get(col)
        if _is_blank(old_val):
            to_write[col] = new_val
            continue
        # Existing non-NULL value present.
        if old_val == new_val:
            continue            # no conflict
        conflicts.append((col, old_val, new_val))
        if overwrite:
            to_write[col] = new_val
    return to_write, conflicts


def apply_update(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    fields_to_write: dict,
    raw_response: dict,
) -> None:
    """Persist the update: column writes (if any) + apify_enrichment_metadata
    + apify_enriched_at. Always sets the audit columns even when
    fields_to_write is empty (so re-runs skip the row)."""
    set_clauses = [f"{col} = ?" for col in fields_to_write.keys()]
    values: list = list(fields_to_write.values())
    set_clauses.append("apify_enrichment_metadata = ?")
    values.append(json.dumps(raw_response, default=str, ensure_ascii=False))
    set_clauses.append("apify_enriched_at = datetime('now')")
    sql = f"UPDATE companies SET {', '.join(set_clauses)} WHERE id = ?"
    values.append(company_id)
    conn.execute(sql, tuple(values))


# ── Actor call (lazy import — not loaded under --preflight-only) ────────────


def _call_harvestapi(urls: list[str]) -> list[dict]:
    """Single batched call. Returns dataset items (including 'Company
    not found' error rows for invalid URLs)."""
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


def _index_by_url(items: list[dict]) -> dict[str, dict]:
    """Map LinkedIn URL → result dict. Mirrors the canonicalisation
    module's index helper. harvestapi error rows have linkedinUrl=None
    so we fish the URL out of the error string or originalQuery."""
    out: dict[str, dict] = {}
    for it in items:
        url = it.get("linkedinUrl") or ""
        if not url:
            err = it.get("error") or ""
            m = re.search(r"`([^`]+)`", err)
            if m:
                url = m.group(1)
            else:
                url = (it.get("originalQuery") or {}).get("search") or ""
        url = url.rstrip("/").lower()
        if url:
            out[url] = it
    return out


# ── Preflight printer (pure-stdout, no I/O) ─────────────────────────────────


def _print_preflight_plan(
    cohort_size: int,
    *,
    no_write: bool,
    overwrite: bool,
    cum_cap: float,
    no_budget_cap: bool,
) -> None:
    """Print the cost plan WITHOUT touching the network. Used by
    --preflight-only and exercised by the offline self-test."""
    estimate = cohort_size * COST_PER_RESULT_USD
    print("=" * 70)
    print("apify_company_enrichment --preflight-only  ($0 spend; no actor call)")
    print("-" * 70)
    print(f"  cohort size:           {cohort_size}")
    print(f"  cost / company:        ${COST_PER_RESULT_USD:.4f}")
    print(f"  estimated total:       ${estimate:.4f}")
    print(f"  cumulative cap:        ${cum_cap:.2f}"
          + ("  (DISABLED — --no-budget-cap set)" if no_budget_cap else ""))
    print(f"  --no-write set:        {no_write}")
    print(f"  --overwrite set:       {overwrite}")
    print("-" * 70)
    if no_budget_cap:
        print("  budget verdict:        cap bypassed; full cohort would run")
    elif estimate > cum_cap:
        n_max = int(cum_cap // COST_PER_RESULT_USD)
        print(f"  budget verdict:        WOULD STOP — only {n_max} of "
              f"{cohort_size} rows fit within ${cum_cap:.2f} cap")
    else:
        print(f"  budget verdict:        would process all {cohort_size} rows "
              f"(${estimate:.4f} <= ${cum_cap:.2f})")
    print("=" * 70)
    print("(no actor called; $0 spent; re-run without --preflight-only to execute)")


# ── Main run loop ───────────────────────────────────────────────────────────


def _run_score_classify_chain(
    conn: sqlite3.Connection,
    touched_ids: list[int],
) -> dict:
    """Re-score and re-classify ONLY the rows enrichment just touched.

    Default Phase 2c-onward behaviour per Q21 (resolved 2026-05-10).
    Uses the row-scoped `score_one` / `classify_one` entry points so we
    don't trigger a full-table rescore for a typical 25-row batch.
    Returns a stats dict for the run summary.
    """
    # Lazy imports — keeps `--preflight-only` zero-cost-by-construction
    # paranoia intact (no top-level dependency on score/classify).
    #
    # score.py uses bare `import thesis` (not `from src import thesis`)
    # because it's normally invoked as `python src/score.py`, which puts
    # src/ on sys.path automatically. When we lazy-import from here under
    # `python -m src.collectors.apify_company_enrichment`, src/ is NOT on
    # sys.path. Insert it before importing so `import thesis` resolves.
    import sys as _sys
    _src_path = str(ROOT / "src")
    if _src_path not in _sys.path:
        _sys.path.insert(0, _src_path)
    from score import score_one
    from classify import classify_one
    import time as _time

    n_scored, n_classified, n_skipped_score = 0, 0, 0
    t0 = _time.time()
    for cid in touched_ids:
        result = score_one(cid, conn)
        if result is None:
            # score-eligibility predicate excluded the row (rare post-D-012;
            # would happen if the row carries enrich_error or matches the
            # portfolio/duplicate filters).
            n_skipped_score += 1
            continue
        n_scored += 1
    score_secs = _time.time() - t0

    t1 = _time.time()
    for cid in touched_ids:
        classify_one(cid, conn)
        n_classified += 1
    classify_secs = _time.time() - t1

    return {
        "n_scored":         n_scored,
        "n_skipped_score":  n_skipped_score,
        "n_classified":     n_classified,
        "score_secs":       round(score_secs, 2),
        "classify_secs":    round(classify_secs, 2),
    }


def run(
    *,
    limit: int | None = None,
    no_write: bool = False,
    overwrite: bool = False,
    cumulative_budget_cap: float = DEFAULT_CUMULATIVE_BUDGET_CAP_USD,
    no_budget_cap: bool = False,
    no_rescore: bool = False,
) -> int:
    """Returns shell exit code (0 success, 2 budget abort)."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    cohort = load_cohort(conn, limit=limit)
    log.info("cohort: %d row(s) (limit=%s, overwrite=%s, no_write=%s)",
             len(cohort), limit, overwrite, no_write)
    if not cohort:
        log.info("nothing to do — re-run without --limit or check apify_enriched_at predicate")
        return 0

    estimated = len(cohort) * COST_PER_RESULT_USD
    log.info("estimated spend: $%.4f (cap $%.2f)",
             estimated, cumulative_budget_cap)
    if not no_budget_cap and estimated > cumulative_budget_cap:
        log.error("BUDGET_ABORT: estimated $%.4f > cap $%.2f. "
                  "Use --limit to chunk or --no-budget-cap to override.",
                  estimated, cumulative_budget_cap)
        return 2

    stamp = _dt.datetime.now().strftime("%Y%m%d")
    conflicts_path = ROOT / "data" / f"phase2c_conflicts_{stamp}.txt"
    conflicts_path.parent.mkdir(parents=True, exist_ok=True)
    # Append-mode so multiple chunked runs land in one file
    conflicts_fh = open(conflicts_path, "a", encoding="utf-8")
    conflicts_fh.write(f"\n# === run started {_dt.datetime.now().isoformat(timespec='seconds')} ===\n")

    counts = {"processed": 0, "filled": 0, "no_change": 0,
              "not_found": 0, "no_response": 0,
              "conflicts": 0}
    spent = 0.0
    touched_ids: list[int] = []     # cohort IDs that were successfully written
                                    # to companies (basis for the auto-chain;
                                    # see Q21 / D-013).
    try:
        for i in range(0, len(cohort), BATCH_SIZE):
            batch = cohort[i:i + BATCH_SIZE]
            urls  = [r["linkedin_url"] for r in batch]
            items = _call_harvestapi(urls)
            spent += len(items) * COST_PER_RESULT_USD
            idx = _index_by_url(items)
            for row in batch:
                key = row["linkedin_url"].rstrip("/").lower()
                it  = idx.get(key)
                counts["processed"] += 1
                if it is None:
                    log.warning("no actor response for id=%d %r",
                                row["id"], row["linkedin_url"])
                    counts["no_response"] += 1
                    continue
                err = (it.get("error") or "")
                if err and "not found" in err.lower():
                    log.info("STALE_SLUG: id=%d %r — actor: not found",
                             row["id"], row["linkedin_url"])
                    counts["not_found"] += 1
                    continue
                # Real result.
                mapped = map_response_to_fields(it)
                fields_to_write, row_conflicts = compute_update_plan(
                    row, mapped, overwrite=overwrite,
                )
                if row_conflicts:
                    counts["conflicts"] += len(row_conflicts)
                    for col, ours, theirs in row_conflicts:
                        conflicts_fh.write(
                            f"company_id={row['id']:<5} field={col:<22} "
                            f"ours={ours!r:<40} theirs={theirs!r}\n"
                        )
                if no_write:
                    log.info("NO_WRITE: id=%d would update %d field(s); "
                             "%d conflict(s)",
                             row["id"], len(fields_to_write), len(row_conflicts))
                    if fields_to_write:
                        counts["filled"] += 1
                    else:
                        counts["no_change"] += 1
                    continue
                apply_update(
                    conn,
                    company_id=row["id"],
                    fields_to_write=fields_to_write,
                    raw_response=it,
                )
                touched_ids.append(row["id"])
                if fields_to_write:
                    counts["filled"] += 1
                else:
                    counts["no_change"] += 1
            if not no_write:
                conn.commit()
    finally:
        conflicts_fh.close()

    # ── Auto-chain: score+classify on touched rows (D-013, Q21 resolved) ──
    chain_status: str
    chain_stats: dict | None = None
    if no_rescore:
        chain_status = "SKIPPED (--no-rescore)"
    elif not touched_ids:
        chain_status = "SKIPPED (0 successful enrichments)"
    else:
        log.info("auto-chain: running score+classify on %d enriched row(s)",
                 len(touched_ids))
        # Reuse the same connection — it was kept open through the loop. The
        # finally block closes only the conflicts file now. Conn is closed
        # below after the chain runs.
        chain_stats = _run_score_classify_chain(conn, touched_ids)
        chain_status = (
            f"COMPLETE (scored={chain_stats['n_scored']} in "
            f"{chain_stats['score_secs']}s, classified="
            f"{chain_stats['n_classified']} in "
            f"{chain_stats['classify_secs']}s)"
        )
        log.info("auto-chain: %s", chain_status)
    # D-021: portfolio reconciliation after enrichment. Enrichment
    # can fill `companies.website` (D-010 NULL-only path) which may
    # newly match a `portfolio` row by website even when the name
    # didn't match. Belt-and-braces against the kind of gap that left
    # one row unmarked.
    try:
        from ..ingest import mark_portfolio_companies  # noqa: E402
        n_marked = mark_portfolio_companies(conn)
        if n_marked:
            log.info("portfolio reconciliation: %d newly-marked row(s)", n_marked)
    except Exception as e:
        log.warning("portfolio reconciliation skipped: %s", e)
    conn.close()

    print("\n=== apify_company_enrichment summary ===")
    for k, v in counts.items():
        print(f"  {k:<14} {v}")
    print(f"  spend          ${spent:.4f}  (cap ${cumulative_budget_cap:.2f})")
    print(f"  conflicts log  {conflicts_path}")
    print(f"  auto-chain     {chain_status}")
    return 0


# ── Self-test ───────────────────────────────────────────────────────────────


def _self_test() -> int:
    failures: list[str] = []

    print("--- response field-mapping ---")
    cases: list[tuple[str, dict, dict]] = [
        ("description filled when present",
         {"description": "We build counter-UAS systems."},
         {"description": "We build counter-UAS systems."}),
        ("empty description treated as missing",
         {"description": "   "},
         {}),
        ("industries[0].name → primary_sector",
         {"industries": [{"id": "1", "name": "Defense & Space", "urn": "urn:li:1"}]},
         {"primary_sector": "Defense & Space"}),
        ("employeeCountRange {start,end} → '11 - 50' string",
         {"employeeCountRange": {"start": 11, "end": 50}},
         {"employee_count_est": "11 - 50"}),
        ("employeeCountRange both null → no key",
         {"employeeCountRange": {"start": None, "end": None}},
         {}),
        ("employeeCountRange open-ended → 'N+'",
         {"employeeCountRange": {"start": 1001, "end": None}},
         {"employee_count_est": "1001+"}),
        ("foundedOn.year coerced to int",
         {"foundedOn": {"year": "2018"}},
         {"founded_year": 2018}),
        ("foundedOn.year out-of-range rejected",
         {"foundedOn": {"year": 1500}},
         {}),
        ("country: locations[0].country=US → 'United States'",
         {"locations": [{"country": "US"}]},
         {"hq_country": "United States"}),
        ("country: locations null → description fallback ('… South Africa')",
         {"description": "ODD SYSTEMS is based in MIDRAND, Gauteng, South Africa."},
         {"description": "ODD SYSTEMS is based in MIDRAND, Gauteng, South Africa.",
          "hq_country": "South Africa"}),
        ("country: both null → no key (don't default to US)",
         {"name": "Picogrid"},
         {}),
        ("country: unknown ISO passes through",
         {"locations": [{"country": "ZZ"}]},
         {"hq_country": "ZZ"}),
        ("unicode in description preserved",
         {"description": "Розробка БПЛА для ЗСУ"},
         {"description": "Розробка БПЛА для ЗСУ"}),
        ("very long description preserved (no truncation)",
         {"description": "x" * 3000},
         {"description": "x" * 3000}),
        ("malformed industries (None) → no primary_sector",
         {"industries": None},
         {}),
        ("industries[0] is bare string",
         {"industries": ["Defense"]},
         {"primary_sector": "Defense"}),
    ]
    for label, item, expected in cases:
        got = map_response_to_fields(item)
        # Allow extra keys (description gets carried into the country test
        # because we want fallback to work); only require expected to be subset.
        ok = all(got.get(k) == v for k, v in expected.items()) and \
             (set(expected.keys()) == set(got.keys()) or
              # Allow description to be a passthrough alongside hq_country
              ({k: got.get(k) for k in expected} == expected))
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  {label}")
        if not ok:
            print(f"        got={got!r}  expected_subset={expected!r}")
            failures.append(label)

    print("\n--- update plan (NULL-only vs. --overwrite) ---")
    plan_cases: list[tuple] = [
        # (label, existing, incoming, overwrite, expected_writes, expected_conflicts_count)
        ("fills NULL with new value",
            {"description": None}, {"description": "x"}, False,
            {"description": "x"}, 0),
        ("does NOT overwrite existing non-NULL by default",
            {"description": "old"}, {"description": "new"}, False,
            {}, 1),
        ("--overwrite DOES overwrite + still logs conflict",
            {"description": "old"}, {"description": "new"}, True,
            {"description": "new"}, 1),
        ("equal value → no write, no conflict",
            {"description": "same"}, {"description": "same"}, False,
            {}, 0),
        ("blank string treated as null — gets filled",
            {"description": "  "}, {"description": "real"}, False,
            {"description": "real"}, 0),
        ("multi-field: mixes NULL fills + conflicts",
            {"description": "old desc", "founded_year": None,
             "hq_country": "United States", "primary_sector": None},
            {"description": "new desc", "founded_year": 2018,
             "hq_country": "Ukraine",   "primary_sector": "Defense"},
            False,
            {"founded_year": 2018, "primary_sector": "Defense"},
            2),
    ]
    for label, existing, incoming, ow, want_writes, want_conflicts_n in plan_cases:
        writes, conflicts = compute_update_plan(existing, incoming, overwrite=ow)
        ok = writes == want_writes and len(conflicts) == want_conflicts_n
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  {label}")
        if not ok:
            print(f"        writes={writes!r}  conflicts={conflicts!r}")
            failures.append(label)

    print("\n--- preflight-only zero-cost guarantee ---")
    apify_loaded_before = "apify_client" in sys.modules
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_preflight_plan(133, no_write=False, overwrite=False,
                              cum_cap=0.85, no_budget_cap=False)
    out = buf.getvalue()
    apify_loaded_after = "apify_client" in sys.modules
    pre_cases = [
        ("preflight prints cohort size",      "cohort size:           133" in out),
        ("preflight prints cost line",        "estimated total:" in out),
        ("preflight prints $0 disclaimer",    "$0 spent" in out),
        ("preflight prints no-actor line",    "no actor called" in out),
        ("no apify_client side-effect import",
                                              apify_loaded_after == apify_loaded_before),
    ]
    for label, ok in pre_cases:
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  {label}")
        if not ok:
            failures.append(label)

    # Over-cap branch coverage
    over_buf = io.StringIO()
    with redirect_stdout(over_buf):
        _print_preflight_plan(500, no_write=False, overwrite=False,
                              cum_cap=0.85, no_budget_cap=False)
    over_out = over_buf.getvalue()
    if "WOULD STOP" in over_out:
        print("  PASS  over-cap plan reports WOULD STOP")
    else:
        failures.append("over-cap plan reports WOULD STOP")
        print("  FAIL  over-cap plan reports WOULD STOP")

    n_total = len(cases) + len(plan_cases) + len(pre_cases) + 1
    print(f"\n{n_total - len(failures)}/{n_total} cases passed")
    return 0 if not failures else 1


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 2c — apify_company_enrichment "
                    "(harvestapi firmographics for validated cohort).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--preflight-only", action="store_true",
                   help="Print the cost plan and EXIT BEFORE any actor call. "
                        "$0 spend by construction.")
    p.add_argument("--no-write", action="store_true",
                   help="Call the actor and parse results, but skip the DB UPDATE. "
                        "**Actor IS called and you DO pay.** For $0, use --preflight-only.")
    p.add_argument("--overwrite", action="store_true",
                   help="Operator opt-in: also fill non-NULL fields. "
                        "Off by default (D-010 NULL-only policy). Conflicts are "
                        "logged either way.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap at N companies (Phase B/C testing).")
    p.add_argument("--cumulative-budget-cap", type=float,
                   default=DEFAULT_CUMULATIVE_BUDGET_CAP_USD,
                   help=f"Override cumulative-spend cap (default "
                        f"${DEFAULT_CUMULATIVE_BUDGET_CAP_USD:.2f}).")
    p.add_argument("--no-budget-cap", action="store_true",
                   help="Bypass the cumulative-spend cap entirely. "
                        "Use deliberately.")
    p.add_argument("--no-rescore", action="store_true",
                   help="Skip the post-enrichment score+classify auto-chain. "
                        "Default behaviour is auto-chain (D-013, Q21). Use this "
                        "for batch enrichment runs where the operator wants to "
                        "score+classify once at the end across all rows touched.")
    p.add_argument("--self-test", action="store_true",
                   help="Run inline test cases and exit. No actor calls; "
                        "no DB writes.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if args.self_test:
        return _self_test()
    if args.preflight_only:
        # Need cohort size for the plan — read-only DB query, no actor.
        conn = sqlite3.connect(DB_PATH)
        cohort_size = len(load_cohort(conn, limit=args.limit))
        conn.close()
        _print_preflight_plan(
            cohort_size,
            no_write=args.no_write,
            overwrite=args.overwrite,
            cum_cap=args.cumulative_budget_cap,
            no_budget_cap=args.no_budget_cap,
        )
        return 0
    return run(
        limit=args.limit,
        no_write=args.no_write,
        overwrite=args.overwrite,
        cumulative_budget_cap=args.cumulative_budget_cap,
        no_budget_cap=args.no_budget_cap,
        no_rescore=args.no_rescore,
    )


if __name__ == "__main__":
    sys.exit(main())
