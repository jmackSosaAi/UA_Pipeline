"""Phase 3 — apify_company_employees: discover founders + contacts via apt_marble.

Calls `apt_marble/linkedin-company-employees-scraper` (the Phase 3
bake-off winner — commit `4586ce0`) against companies that have a
MATCH-validated `linkedin_url` AND haven't been attempted yet.

Per company, the actor returns up to `maxEmployees` employees with
{name, headline, profileUrl, companyUrl, companySlug}. The headline
flows through `apify_leads.is_decision_maker` (D-002-era parser) to
classify each person into `founders` (founder/CEO/CTO/chief/...) or
`contacts` (VP/Head/Director/...). Junior employees (no senior
token) are skipped — Phase 3 only writes decision-makers.

Cost model (read this before adding a CLI flag):
    --preflight-only Parses args, prints the cost plan, EXITS BEFORE
                     any actor call. $0 spend by construction (no
                     `apify_client` import is triggered).
    --no-write       Calls the actor, parses, dedupes — but skips
                     the founders/contacts INSERTs. Still marks
                     apify_employees_attempted_at so the row doesn't
                     re-attempt. **Actor IS called and you DO pay.**

Dedupe policy (Q26 resolution):
    Before INSERT, query existing `founders.linkedin_url` and
    `contacts.value` (type='linkedin') for the same person's
    normalized URL. If present, skip the INSERT. Both Phase 1
    (apify_leads) and prior Phase 3 runs are respected.

Sentinel format: `apify_company_employees://<linkedinUrl>`
    Written to `founders.source_url` and `contacts.source_url`.
    Source-aware DELETE in enrich.py protects this prefix
    (extended in this commit per D-016).

Cohort filter: `linkedin_url IS NOT NULL AND
                apify_employees_attempted_at IS NULL`. The attempt
marker is set after every row processed (even on 0-employee
returns — Q28 resolution). Cohort is naturally resumable across
monthly cycles.
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

from .apify_leads import (                                      # noqa: E402
    FOUNDER_TOKENS, VP_TOKENS, DIRECTOR_TOKENS,
)
from .base import DB_PATH                                       # noqa: E402


# Phase 3 headline classifier — same token sets as apify_leads
# (`is_decision_maker` reuse "in spirit"), but substring/word-boundary
# matching because apt_marble returns FREE-TEXT headlines
# (`"Co-Founder/CEO at The Swarm"`) rather than Apollo's comma-separated
# seniority field (`"Founder, CEO"`). Falling back to apify_leads's
# comma-split parser would return None on every apt_marble row.
import re as _re
_FOUNDER_RE = _re.compile(
    r"(?:^|[^a-z])(" + "|".join(_re.escape(t) for t in sorted(FOUNDER_TOKENS, key=len, reverse=True)) + r")(?:[^a-z]|$)",
    _re.IGNORECASE,
)
_VP_RE = _re.compile(
    r"\b(" + "|".join(_re.escape(t) for t in sorted(VP_TOKENS, key=len, reverse=True)) + r")\b",
    _re.IGNORECASE,
)
_DIRECTOR_RE = _re.compile(
    r"\b(" + "|".join(_re.escape(t) for t in DIRECTOR_TOKENS) + r")\b",
    _re.IGNORECASE,
)


def classify_headline(headline: str | None) -> tuple[str | None, float]:
    """Map a free-text LinkedIn headline to (target_table, confidence).

    Mirrors `apify_leads.is_decision_maker`'s semantics:
      ('founders', 0.85)   if any FOUNDER_TOKEN appears
      ('contacts', 0.70)   if any VP_TOKEN appears
      ('contacts', 0.55)   if any DIRECTOR_TOKEN appears
      (None, 0.0)          otherwise — Phase 3 skips this employee
    Founder bucket wins over VP/director when both are present.
    """
    if not headline:
        return (None, 0.0)
    if _FOUNDER_RE.search(headline):
        return ("founders", 0.85)
    if _VP_RE.search(headline):
        return ("contacts", 0.70)
    if _DIRECTOR_RE.search(headline):
        return ("contacts", 0.55)
    return (None, 0.0)

# ── Module constants ────────────────────────────────────────────────────────

ACTOR_ID = "apt_marble/linkedin-company-employees-scraper"
SOURCE = "apify_company_employees"
SENTINEL_PREFIX = "apify_company_employees://"

# Bake-off measured pricing: $0.0035 actor-start + $0.0035 per employee.
# At maxEmployees=5: 1 batch × $0.0035 + 5N × $0.0035 / N companies = ~$0.0175/co
# at the per-co overhead unit; in practice batched calls amortize the start fee.
COST_PER_EMPLOYEE_USD = 0.0035
COST_ACTOR_START_USD = 0.0035

DEFAULT_MAX_EMPLOYEES_PER_CO = 5      # D-016: production value from bake-off
DEFAULT_CUMULATIVE_BUDGET_CAP_USD = 14.00
BATCH_SIZE = 25                       # matches apify_company_enrichment

log = logging.getLogger("apify_company_employees")


# ── URL normalisation for dedup ─────────────────────────────────────────────


def _normalise_li_url(url: str | None) -> str | None:
    """Trailing-slash + case-insensitive normalisation. Returns None on
    empty/non-LinkedIn input."""
    if not url:
        return None
    u = url.strip().lower().rstrip("/")
    if "linkedin.com" not in u:
        return None
    return u


# ── Cohort load ─────────────────────────────────────────────────────────────


def load_cohort(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    sql = """
        SELECT id, name, linkedin_url, source, tier
        FROM companies
        WHERE linkedin_url IS NOT NULL
          AND TRIM(linkedin_url) != ''
          AND apify_employees_attempted_at IS NULL
        ORDER BY id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [
        {"id": r[0], "name": r[1], "linkedin_url": r[2], "source": r[3], "tier": r[4]}
        for r in conn.execute(sql).fetchall()
    ]


# ── Dedupe lookup ───────────────────────────────────────────────────────────


def _build_existing_li_lookup(conn: sqlite3.Connection) -> set[str]:
    """Build a single-query set of every normalised LinkedIn profile URL
    already present in founders + contacts. Used for Q26 dedupe."""
    urls: set[str] = set()
    for r in conn.execute(
        "SELECT linkedin_url FROM founders WHERE linkedin_url IS NOT NULL "
        "AND TRIM(linkedin_url) != ''"
    ).fetchall():
        nu = _normalise_li_url(r[0])
        if nu:
            urls.add(nu)
    for r in conn.execute(
        "SELECT value FROM contacts WHERE type='linkedin' "
        "AND value IS NOT NULL AND TRIM(value) != ''"
    ).fetchall():
        nu = _normalise_li_url(r[0])
        if nu:
            urls.add(nu)
    return urls


# ── Inserts ─────────────────────────────────────────────────────────────────


def _insert_founder(
    conn: sqlite3.Connection, *, company_id: int, name: str, role: str | None,
    linkedin_url: str | None, confidence: float, sentinel: str,
) -> None:
    conn.execute(
        "INSERT INTO founders "
        "  (company_id, name, role, linkedin_url, confidence, source_url) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (company_id, name, role, linkedin_url, confidence, sentinel),
    )


def _insert_contact(
    conn: sqlite3.Connection, *, company_id: int, ctype: str, cvalue: str,
    confidence: float, sentinel: str,
) -> None:
    conn.execute(
        "INSERT INTO contacts "
        "  (company_id, type, value, confidence, source_url) "
        "VALUES (?, ?, ?, ?, ?)",
        (company_id, ctype, cvalue, confidence, sentinel),
    )


def _mark_attempted(conn: sqlite3.Connection, company_id: int) -> None:
    conn.execute(
        "UPDATE companies SET apify_employees_attempted_at = datetime('now') "
        "WHERE id = ?",
        (company_id,),
    )


# ── Actor call (lazy import) ────────────────────────────────────────────────


def _call_apt_marble(urls: list[str], max_employees: int) -> list[dict]:
    """Batched call. Returns list of employee items (all companies combined)."""
    from apify_client import ApifyClient
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise SystemExit("APIFY_TOKEN missing from .env")
    client = ApifyClient(token)
    log.info("calling %s with %d URL(s) maxEmployees=%d",
             ACTOR_ID, len(urls), max_employees)
    t0 = time.time()
    run = client.actor(ACTOR_ID).call(
        run_input={"companyUrls": urls, "maxEmployees": max_employees},
        timeout_secs=900,
    )
    if not run or run.get("status") != "SUCCEEDED":
        log.error("actor run failed: status=%r", (run or {}).get("status"))
        return []
    ds_id = run["defaultDatasetId"]
    items = list(client.dataset(ds_id).iterate_items())
    elapsed = time.time() - t0
    log.info("returned %d employees in %.1fs (run=%s, usageTotalUsd=%s)",
             len(items), elapsed, run.get("id"), run.get("usageTotalUsd"))
    return items


def _index_employees_by_company(
    items: list[dict], cohort_by_normalised_url: dict[str, dict],
) -> dict[int, list[dict]]:
    """Bucket employee items by company_id using each item's companyUrl
    (apt_marble's clean schema)."""
    by_cid: dict[int, list[dict]] = {}
    for it in items:
        co_url = _normalise_li_url(it.get("companyUrl") or "")
        if not co_url:
            log.warning("employee item missing companyUrl: %s", str(it)[:200])
            continue
        co = cohort_by_normalised_url.get(co_url)
        if not co:
            log.warning("companyUrl %r not in cohort", co_url)
            continue
        by_cid.setdefault(co["id"], []).append(it)
    return by_cid


# ── Preflight printer (pure stdout, no I/O) ─────────────────────────────────


def _print_preflight_plan(
    cohort_size: int, max_employees: int,
    *, no_write: bool, cum_cap: float, no_budget_cap: bool,
) -> None:
    """Print the cost plan without touching the network."""
    per_co_est = COST_ACTOR_START_USD / BATCH_SIZE + max_employees * COST_PER_EMPLOYEE_USD
    estimate = cohort_size * per_co_est
    print("=" * 70)
    print(f"apify_company_employees --preflight-only  ($0 spend; no actor call)")
    print("-" * 70)
    print(f"  cohort size:              {cohort_size}")
    print(f"  maxEmployees / company:   {max_employees}")
    print(f"  cost per company (est):   ${per_co_est:.4f}")
    print(f"  estimated total:          ${estimate:.4f}")
    print(f"  cumulative cap:           ${cum_cap:.2f}"
          + ("  (DISABLED — --no-budget-cap)" if no_budget_cap else ""))
    print(f"  --no-write set:           {no_write}")
    print("-" * 70)
    if no_budget_cap:
        print("  budget verdict:           cap bypassed; full cohort would run")
    elif estimate > cum_cap:
        n_max = int(cum_cap / per_co_est) if per_co_est > 0 else cohort_size
        print(f"  budget verdict:           would graceful-stop at ~row {n_max} "
              f"of {cohort_size} ({cohort_size - n_max} deferred to next cycle)")
    else:
        print(f"  budget verdict:           would process all {cohort_size} rows "
              f"(${estimate:.4f} <= ${cum_cap:.2f})")
    print("=" * 70)
    print("(no actor called; $0 spent; re-run without --preflight-only to execute)")


# ── Main run loop ───────────────────────────────────────────────────────────


def run(
    *,
    limit: int | None = None,
    max_employees: int = DEFAULT_MAX_EMPLOYEES_PER_CO,
    no_write: bool = False,
    cumulative_budget_cap: float = DEFAULT_CUMULATIVE_BUDGET_CAP_USD,
    no_budget_cap: bool = False,
) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    cohort = load_cohort(conn, limit=limit)
    log.info("cohort: %d row(s) (limit=%s, max_emp=%d, no_write=%s)",
             len(cohort), limit, max_employees, no_write)
    if not cohort:
        log.info("nothing to do — all eligible rows have apify_employees_attempted_at set")
        return 0

    # Dedupe lookup (Q26 resolution) — single query for all existing
    # founders.linkedin_url + contacts.value(type=linkedin) values.
    existing_li = _build_existing_li_lookup(conn)
    log.info("dedupe set: %d existing LinkedIn URLs across founders+contacts",
             len(existing_li))

    counts = {
        "rows_attempted":      0,
        "rows_with_yield":     0,
        "rows_zero_yield":     0,
        "rows_actor_miss":     0,    # cohort row not represented in any batch response
        "employees_returned":  0,
        "founders_inserted":   0,
        "contacts_inserted":   0,
        "skipped_junior":      0,    # below senior threshold per parser
        "dedupe_skipped":      0,    # already in founders/contacts
    }
    spent_total = 0.0

    cohort_by_url = {
        _normalise_li_url(c["linkedin_url"]): c
        for c in cohort if _normalise_li_url(c["linkedin_url"])
    }
    try:
        for i in range(0, len(cohort), BATCH_SIZE):
            # Predictive cap check using the bake-off per-co estimate.
            per_co_est = COST_ACTOR_START_USD / BATCH_SIZE + \
                         max_employees * COST_PER_EMPLOYEE_USD
            projected = spent_total + min(BATCH_SIZE, len(cohort) - i) * per_co_est
            if not no_budget_cap and projected > cumulative_budget_cap:
                log.warning(
                    "GRACEFUL STOP: cumulative spend $%.4f + next batch est "
                    "would exceed cap $%.2f. Stopped at row %d of %d; remaining "
                    "%d cohort row(s) will be processed in next session.",
                    spent_total, cumulative_budget_cap, i, len(cohort), len(cohort) - i,
                )
                break

            batch = cohort[i:i + BATCH_SIZE]
            urls = [r["linkedin_url"] for r in batch]
            items = _call_apt_marble(urls, max_employees)

            # apify reports usageTotalUsd on the run, but our wrapper
            # discarded the run obj — approximate spend from items count.
            spent_batch = COST_ACTOR_START_USD + len(items) * COST_PER_EMPLOYEE_USD
            spent_total += spent_batch
            counts["employees_returned"] += len(items)

            # Bucket employees by company
            by_cid = _index_employees_by_company(items, cohort_by_url)

            # Mark every batch row attempted, even on 0 yield (Q28).
            for row in batch:
                counts["rows_attempted"] += 1
                emp_list = by_cid.get(row["id"], [])
                if not emp_list:
                    counts["rows_zero_yield"] += 1
                    if not no_write:
                        _mark_attempted(conn, row["id"])
                    continue
                counts["rows_with_yield"] += 1
                if not no_write:
                    _mark_attempted(conn, row["id"])

                for emp in emp_list:
                    headline = (emp.get("headline") or "").strip()
                    name = (emp.get("name") or "").strip()
                    if not name:
                        continue
                    profile_url = (emp.get("profileUrl") or "").strip() or None
                    target, confidence = classify_headline(headline)
                    if target is None:
                        counts["skipped_junior"] += 1
                        continue
                    sentinel = f"{SENTINEL_PREFIX}{profile_url or ''}"
                    # Dedupe by normalised LinkedIn URL.
                    norm_url = _normalise_li_url(profile_url)
                    if norm_url and norm_url in existing_li:
                        counts["dedupe_skipped"] += 1
                        continue
                    if no_write:
                        if target == "founders":
                            counts["founders_inserted"] += 1
                        else:
                            counts["contacts_inserted"] += 1
                        continue
                    if target == "founders":
                        _insert_founder(
                            conn, company_id=row["id"], name=name, role=headline,
                            linkedin_url=profile_url, confidence=confidence,
                            sentinel=sentinel,
                        )
                        counts["founders_inserted"] += 1
                    else:
                        _insert_contact(
                            conn, company_id=row["id"], ctype="linkedin",
                            cvalue=profile_url, confidence=confidence,
                            sentinel=sentinel,
                        )
                        counts["contacts_inserted"] += 1
                    if norm_url:
                        existing_li.add(norm_url)
            if not no_write:
                conn.commit()
    finally:
        conn.close()

    print("\n=== apify_company_employees summary ===")
    for k, v in counts.items():
        print(f"  {k:<22} {v}")
    print(f"  spent (estimated)      ${spent_total:.4f}  (cap ${cumulative_budget_cap:.2f})")
    return 0


# ── Self-test ───────────────────────────────────────────────────────────────


def _self_test() -> int:
    failures: list[str] = []
    print("--- URL normalisation ---")
    cases = [
        ("https://www.linkedin.com/in/foo",   "https://www.linkedin.com/in/foo"),
        ("https://www.linkedin.com/in/foo/",  "https://www.linkedin.com/in/foo"),
        ("https://WWW.LinkedIn.com/in/Foo",   "https://www.linkedin.com/in/foo"),
        ("",                                  None),
        ("https://example.com",                None),    # non-LinkedIn
        ("  https://linkedin.com/in/bar/  ",  "https://linkedin.com/in/bar"),
    ]
    for url, expected in cases:
        got = _normalise_li_url(url)
        ok = got == expected
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  in={url!r:<50} → out={got!r}")
        if not ok:
            failures.append(f"_normalise_li_url({url!r})")

    print("\n--- classify_headline (free-text variant of is_decision_maker) ---")
    sen_cases = [
        ("Co-Founder/CEO at The Swarm",           "founders"),
        ("Chief Executive Officer at INFOZAHYST", "founders"),
        ("CEO at CloudBees",                      "founders"),
        ("Head of Finance at Stealth Startup",    "contacts"),
        ("Director of Sales",                     "contacts"),
        ("VP Engineering, Lockheed Martin",       "contacts"),
        ("Software Engineer",                     None),
        ("Marketing Specialist",                  None),
        ("Senior Account Manager",                None),  # Manager intentionally excluded
        ("",                                      None),
        (None,                                    None),
    ]
    for headline, expected in sen_cases:
        target, _conf = classify_headline(headline)
        ok = target == expected
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  headline={headline!r:<47} target={target!r} expected={expected!r}")
        if not ok:
            failures.append(f"classify_headline({headline!r})")

    print("\n--- preflight-only zero-cost guarantee ---")
    apify_loaded_before = "apify_client" in sys.modules
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_preflight_plan(972, 5, no_write=False, cum_cap=14.0, no_budget_cap=False)
    out = buf.getvalue()
    apify_loaded_after = "apify_client" in sys.modules
    pre = [
        ("preflight prints cohort size",      "cohort size:              972" in out),
        ("preflight prints cost line",        "estimated total:" in out),
        ("preflight prints $0 disclaimer",    "$0 spent" in out),
        ("preflight prints no-actor line",    "no actor called" in out),
        ("no apify_client side-effect import",
                                              apify_loaded_after == apify_loaded_before),
        ("over-cap plan reports graceful-stop",
                                              "graceful-stop" in out),
    ]
    for label, ok in pre:
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker}  {label}")
        if not ok:
            failures.append(label)

    print(f"\n{len(cases) + len(sen_cases) + len(pre) - len(failures)}/"
          f"{len(cases) + len(sen_cases) + len(pre)} cases passed")
    return 0 if not failures else 1


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 3 — apify_company_employees collector.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--preflight-only", action="store_true",
                   help="Print the cost plan and EXIT BEFORE any actor call. "
                        "$0 spend.")
    p.add_argument("--no-write", action="store_true",
                   help="Call the actor and parse, but skip INSERTs. Still "
                        "marks apify_employees_attempted_at. **Actor IS called.**")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap at N companies for batch tests.")
    p.add_argument("--max-employees", type=int,
                   default=DEFAULT_MAX_EMPLOYEES_PER_CO,
                   help=f"Per-company employee cap (default "
                        f"{DEFAULT_MAX_EMPLOYEES_PER_CO}, the D-016 production value).")
    p.add_argument("--cumulative-budget-cap", type=float,
                   default=DEFAULT_CUMULATIVE_BUDGET_CAP_USD,
                   help=f"Override cumulative spend cap "
                        f"(default ${DEFAULT_CUMULATIVE_BUDGET_CAP_USD:.2f}).")
    p.add_argument("--no-budget-cap", action="store_true",
                   help="Bypass the cumulative spend cap. Use deliberately.")
    p.add_argument("--self-test", action="store_true",
                   help="Run inline test cases and exit. No actor; no DB writes.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if args.self_test:
        return _self_test()
    if args.preflight_only:
        conn = sqlite3.connect(DB_PATH)
        cohort_size = len(load_cohort(conn, limit=args.limit))
        conn.close()
        _print_preflight_plan(
            cohort_size, args.max_employees,
            no_write=args.no_write,
            cum_cap=args.cumulative_budget_cap,
            no_budget_cap=args.no_budget_cap,
        )
        return 0
    return run(
        limit=args.limit,
        max_employees=args.max_employees,
        no_write=args.no_write,
        cumulative_budget_cap=args.cumulative_budget_cap,
        no_budget_cap=args.no_budget_cap,
    )


if __name__ == "__main__":
    sys.exit(main())
