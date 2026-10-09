"""
Apify Leads — defense-company discovery + decision-maker enrichment.

Calls braveleads/leads-finder-linkedin-apollo-leads-generator with one query
per vocabulary term, dedups results by (organizationName, organizationWebsite),
stores each unique organization as a raw lead, and writes
founder/contact rows for the senior decision-makers among the surfaced
people.

Manual-run only — each invocation costs real money. Default budget cap
$0.85 (≈500 leads at $1.70/1k). Override with --no-budget-cap.

Source tagging:
  raw_leads.source           = "apify_leads"
  founders.source_url        = "apify_leads://<linkedinUrl>"
  contacts.source_url        = "apify_leads://<linkedinUrl>"

The "apify_leads://" sentinel matters — enrich.py's _store_founders and
_store_contacts are now source-aware and skip rows with that prefix
during their DELETE-then-INSERT replace cycle, so Apify-discovered data
survives subsequent enrichment passes.

Founder/contact writes require the org to already exist in companies.
For newly-discovered orgs, raw_leads is written but founders/contacts
are skipped on the first run; a subsequent `promote.py` run materializes
the companies row, then the **replay path** backfills founders for free.

Replay path (cost: $0):

    python scripts/_apify_replay.py <RUN_ID>

Apify keeps run datasets indefinitely; iterating an existing dataset
costs nothing — the actor charge happened at the original run. Each
fresh actor invocation **advances the cursor through Apollo's database**
(returns different orgs every time; the actor logs "Resuming scraping
from previous progress"), so you cannot re-fetch a previous batch
cheaply. Instead, the canonical pattern after every run is:

    1. python -m src.collectors.apify_leads --terms "..."   (paid)
    2. python -m src.collectors.promote                     (free; materialises new orgs)
    3. python scripts/_apify_replay.py <RUN_ID>             (free; backfills founders/contacts)

Step 3 attaches founders/contacts to companies that didn't exist when
the actor was first called. This is a first-class part of the
integration architecture, not a workaround — the cursor-advancing
behaviour of the actor makes a single-call defer→promote→retry impossible
without paying twice.

Cost model (read this before adding a CLI flag):
    --preflight-only Parses args, prints the cost plan, EXITS BEFORE any
                     actor call. $0 spend. Always safe.
    --no-write       Suppresses DB writes but the actor IS called and
                     YOU DO PAY. Use this when you want real actor data
                     for schema/shape verification without touching DB
                     state — not as a "dry run". Replaces the old
                     --dry-run flag, which was misleading on the cost
                     question.
    --no-budget-cap  Disables both the per-run preflight cap and the
                     cumulative cap. Real spend; use deliberately.

Usage:
    python -m src.collectors.apify_leads --terms "counter-drone" --max-results-per-term 100 --preflight-only
    python -m src.collectors.apify_leads --terms "counter-drone" --max-results-per-term 100 --no-write
    python -m src.collectors.apify_leads --terms "counter-drone,loitering munition" --max-results-per-term 100
    python -m src.collectors.apify_leads --terms "...,...,..." --max-results-per-term 200 --no-budget-cap
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

try:
    from db.migrate import migrate as migrate_database
except ImportError:  # supports `python -m src.collectors.apify_leads`
    from src.db.migrate import migrate as migrate_database

from .apify_filter import BaselineFilter
from .base import DB_PATH, store_lead
from .dedup import find_exact_match, normalize_name
from .defense_press import _is_excluded
from .source_config import load_excluded_companies  # noqa: F401  (warm cache)

load_dotenv()

# ── Module constants ─────────────────────────────────────────────────────────

SOURCE = "apify_leads"
SENTINEL_PREFIX = "apify_leads://"
ACTOR_ID = "braveleads/leads-finder-linkedin-apollo-leads-generator"

COST_PER_1K_LEADS_USD = 1.70
DEFAULT_BUDGET_CAP_USD = 0.85         # per-run preflight cap (worst-case total)
CUMULATIVE_BUDGET_CAP_USD = 0.85      # actual-spend cap across all terms in run()
                                      # — fires before each term once the running
                                      # total + that term's estimate would breach it

# ── Seniority parsing (Decision 4) ───────────────────────────────────────────
#
# The actor's `seniority` field is title-case and may be compound, e.g.
# "Founder, CEO" or "Senior, Entry Level". Token-by-token after splitting
# on comma + lowercasing gives the cleanest decision rule.

FOUNDER_TOKENS = {
    "founder", "co-founder", "co_founder", "owner",
    "ceo", "cto", "cfo", "coo", "cxo", "chief",
}
VP_TOKENS       = {"vice president", "vp", "head"}
DIRECTOR_TOKENS = {"director"}
# Manager intentionally excluded — too noisy in the POC sample (Project /
# Account / Operations Managers dominate). Quality > volume.


def is_decision_maker(seniority: str | None) -> tuple[str | None, float]:
    """Map an actor seniority string to (target_table, confidence).

    Returns ('founders', 0.85)   for any token in FOUNDER_TOKENS
            ('contacts', 0.70)   for any token in VP_TOKENS
            ('contacts', 0.55)   for any token in DIRECTOR_TOKENS
            (None, 0.0)          → caller skips this person
    Founder bucket wins over VP/Director when both are present in a
    compound seniority (e.g. "Executive, CEO" → founders).
    """
    tokens = {t.strip().lower() for t in (seniority or "").split(",") if t.strip()}
    if tokens & FOUNDER_TOKENS:
        return ("founders", 0.85)
    if tokens & VP_TOKENS:
        return ("contacts", 0.70)
    if tokens & DIRECTOR_TOKENS:
        return ("contacts", 0.55)
    return (None, 0.0)


# ── Logging ──────────────────────────────────────────────────────────────────

log = logging.getLogger("apify_leads")


def _setup_logging() -> Path:
    """Per-run log file at logs/apify_leads_YYYYMMDD_HHMM.log."""
    log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    log_file = log_dir / f"apify_leads_{stamp}.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    # Also stream INFO+ to stdout so --no-write/--preflight-only output is visible
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.addHandler(stream)
    log.propagate = False
    return log_file


# ── Apify call ───────────────────────────────────────────────────────────────


def _run_actor(term: str, max_results: int) -> list[dict]:
    """One actor call per vocabulary term. Returns list of lead items."""
    from apify_client import ApifyClient   # local import — only when running
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise SystemExit("APIFY_TOKEN missing from .env — abort")
    client = ApifyClient(token)

    run_input = {
        "maxResults": int(max_results),
        "contactEmailStatus": "verified",
        "personTitles": ["CEO", "Founder", "Co-Founder", "Chief Executive Officer",
                         "Vice President", "Director", "Head"],
        "industryKeywords": [term],
    }
    log.info("apify: calling %s | term=%r maxResults=%d", ACTOR_ID, term, max_results)
    run = client.actor(ACTOR_ID).call(run_input=run_input)
    if not run or run.get("status") != "SUCCEEDED":
        log.error("apify: actor run failed; status=%r run_id=%r",
                  (run or {}).get("status"), (run or {}).get("id"))
        return []
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    stats = run.get("stats", {}) or {}
    log.info("apify: term=%r returned %d items in %ss (cu=%s)",
             term, len(items),
             stats.get("runTimeSecs", "?"), stats.get("computeUnits", "?"))
    return items


# ── Org-level dedup (in-memory, per Apify call) ──────────────────────────────


def _org_key(item: dict) -> tuple[str, str]:
    """Stable dedup key for one Apify item.

    Matches the user's spec: lowercased + stripped on name; lowercased,
    stripped, trailing-slash-trimmed on website. Empty strings collide,
    which is fine — the org will be skipped downstream because no
    organizationName means no store_lead call.
    """
    name = (item.get("organizationName") or "").strip().lower()
    site = (item.get("organizationWebsite") or "").strip().lower().rstrip("/")
    return (name, site)


def _group_by_org(items: list[dict]) -> dict[tuple[str, str], dict]:
    """Group leads by org, preserving the per-org Apify metadata once and
    collecting all kept leads for that org."""
    by_key: dict[tuple[str, str], dict] = {}
    for it in items:
        key = _org_key(it)
        if not key[0]:                       # missing org name → skip
            continue
        bucket = by_key.setdefault(key, {
            "org_name":    it.get("organizationName") or "",
            "org_website": it.get("organizationWebsite") or "",
            "org_meta":    {
                "industry":     it.get("organizationIndustry"),
                "founded_year": it.get("organizationFoundedYear"),
                "size":         it.get("organizationSize"),
                "city":         it.get("organizationCity"),
                "state":        it.get("organizationState"),
                "country":      it.get("organizationCountry"),
                "linkedin":     it.get("organizationLinkedinUrl"),
                "specialities": it.get("organizationSpecialities"),
                "description":  it.get("organizationDescription"),
            },
            "leads":       [],
        })
        bucket["leads"].append(it)
    return by_key


# ── DB helpers — idempotent founders/contacts writes ─────────────────────────


def _resolve_company_id(
    conn: sqlite3.Connection,
    canonical_name_lookup: dict[str, int],
    org_name: str,
) -> int | None:
    """Return companies.id for an org name via normalized-name match,
    or None if the org isn't yet in companies (caller skips founder writes)."""
    return canonical_name_lookup.get(normalize_name(org_name))


def _build_canonical_name_lookup(conn: sqlite3.Connection) -> dict[str, int]:
    """{normalize_name(companies.name): companies.id} for all companies.

    Built once per run. First-occurrence wins on collision (matches the
    convention used in collectors.defense_press.backfill_company_ids).
    """
    out: dict[str, int] = {}
    for cid, cname in conn.execute("SELECT id, name FROM companies").fetchall():
        if not cname:
            continue
        out.setdefault(normalize_name(cname), cid)
    return out


def _founder_exists(conn: sqlite3.Connection, company_id: int, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM founders WHERE company_id = ? AND LOWER(name) = LOWER(?) LIMIT 1",
        (company_id, name),
    ).fetchone()
    return row is not None


def _contact_exists(
    conn: sqlite3.Connection,
    company_id: int,
    ctype: str,
    cvalue: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM contacts "
        "WHERE company_id = ? AND type = ? AND LOWER(value) = LOWER(?) LIMIT 1",
        (company_id, ctype, cvalue),
    ).fetchone()
    return row is not None


def _insert_founder(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    name: str,
    role: str | None,
    linkedin_url: str | None,
    confidence: float,
    sentinel: str,
) -> None:
    conn.execute(
        """
        INSERT INTO founders
            (company_id, name, role, linkedin_url, confidence, source_url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (company_id, name, role, linkedin_url, confidence, sentinel),
    )
    conn.commit()


def _insert_contact(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    ctype: str,
    cvalue: str,
    confidence: float,
    sentinel: str,
) -> None:
    conn.execute(
        """
        INSERT INTO contacts (company_id, type, value, confidence, source_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        (company_id, ctype, cvalue, confidence, sentinel),
    )
    conn.commit()


# ── Per-org processing ───────────────────────────────────────────────────────


class OrgStats:
    """Per-org bookkeeping for the run summary."""
    __slots__ = (
        "new_lead", "new_canonical", "founders_added", "contacts_added",
        "skipped", "rejected", "rejected_rules",
    )

    def __init__(self) -> None:
        self.new_lead: bool       = False
        self.new_canonical: bool  = False
        self.founders_added: int  = 0
        self.contacts_added: int  = 0
        self.skipped: bool        = False
        self.rejected: bool       = False
        self.rejected_rules: list[str] = []


def _log_rejection(
    conn: sqlite3.Connection,
    *,
    company_name: str,
    failed_rules: list[str],
    raw_metadata: dict,
) -> None:
    """Persist a baseline-filter rejection to rejected_orgs."""
    conn.execute(
        """
        INSERT INTO rejected_orgs
            (company_name, source, rejection_reasons, raw_metadata)
        VALUES (?, ?, ?, ?)
        """,
        (
            company_name,
            SOURCE,
            json.dumps(failed_rules),
            json.dumps(raw_metadata) if raw_metadata else None,
        ),
    )
    conn.commit()


def _process_org(
    bucket: dict,
    conn: sqlite3.Connection,
    canonical_lookup: dict[str, int],
    no_write: bool,
    baseline_filter: BaselineFilter | None = None,
    bypass_filter: bool = False,
) -> OrgStats:
    s = OrgStats()
    org_name    = bucket["org_name"]
    org_website = bucket["org_website"]
    leads       = bucket["leads"]

    # 1. Excluded primes — skip the whole org.
    if _is_excluded(org_name):
        log.info("SKIP_ORG: %r in excluded primes list", org_name)
        s.skipped = True
        return s

    # 2. Baseline filter — reject orgs that don't fit the fund's thesis baseline
    #    (geography, size, age, industry, description noise). Rejections
    #    are persisted to rejected_orgs so the operator can search and
    #    unreject if a rule turns out to be too tight. `bypass_filter`
    #    is for operator-driven backfills where the filter is intentionally
    #    skipped — never set this in scheduled runs.
    if not bypass_filter and baseline_filter is not None:
        result = baseline_filter.evaluate(bucket)
        if not result.passed:
            s.rejected = True
            s.rejected_rules = list(result.failed_rules)
            log.info(
                "REJECT_ORG: %r failed baseline rules=%s",
                org_name, result.failed_rules,
            )
            for reason in result.reasons:
                log.info("    - %s", reason)
            if not no_write:
                _log_rejection(
                    conn,
                    company_name=org_name,
                    failed_rules=result.failed_rules,
                    raw_metadata=bucket.get("org_meta") or {},
                )
            return s

    # 3. raw_leads + assign_canonical (or --no-write logging path).
    org_meta = bucket["org_meta"]
    if no_write:
        log.info(
            "NO_WRITE store_lead: name=%r website=%r leads_at_org=%d",
            org_name, org_website, len(leads),
        )
    else:
        new_id = store_lead(
            company_name=org_name,
            source=SOURCE,
            source_url=None,
            initial_description=(org_meta.get("description") or "")[:500] or None,
            country=org_meta.get("country"),
            website=org_website or None,
            source_metadata=org_meta,
        )
        s.new_lead = (new_id is not None)
        log.info(
            "STORE_LEAD: %r → %s | website=%r leads_at_org=%d",
            org_name,
            f"raw_leads.id={new_id}" if new_id else "DUPLICATE",
            org_website,
            len(leads),
        )

    # 4. Founder/contact writes require an existing companies row.
    company_id = _resolve_company_id(conn, canonical_lookup, org_name)
    if company_id is None:
        log.info(
            "DEFER_PEOPLE: %r has no companies row yet — re-run after promote.py "
            "to backfill %d senior leads",
            org_name,
            sum(1 for ld in leads if is_decision_maker(ld.get("seniority"))[0]),
        )
        return s

    # 5. Per-lead founder/contact writes (idempotent).
    for ld in leads:
        seniority = ld.get("seniority") or ""
        target, confidence = is_decision_maker(seniority)
        if target is None:
            continue

        person_name  = (ld.get("fullName") or "").strip() or (
            f"{(ld.get('firstName') or '').strip()} {(ld.get('lastName') or '').strip()}".strip()
        )
        if not person_name:
            continue
        title    = ld.get("position") or seniority or None
        li_url   = (ld.get("linkedinUrl") or "").strip() or None
        email    = (ld.get("email") or "").strip() or None
        sentinel = f"{SENTINEL_PREFIX}{li_url or ''}"

        if target == "founders":
            if no_write:
                log.info(
                    "NO_WRITE founder: company_id=%d name=%r role=%r conf=%.2f",
                    company_id, person_name, title, confidence,
                )
                s.founders_added += 1
                continue
            if _founder_exists(conn, company_id, person_name):
                log.info(
                    "SKIP_FOUNDER (exists): company_id=%d %r",
                    company_id, person_name,
                )
                continue
            _insert_founder(
                conn,
                company_id=company_id,
                name=person_name,
                role=title,
                linkedin_url=li_url,
                confidence=confidence,
                sentinel=sentinel,
            )
            s.founders_added += 1
            log.info(
                "FOUNDER: company_id=%d %r role=%r conf=%.2f",
                company_id, person_name, title, confidence,
            )
        else:  # target == "contacts"
            # Per spec: linkedin contact + general_email contact (if email).
            if li_url:
                if no_write:
                    log.info(
                        "NO_WRITE contact[linkedin]: company_id=%d %r %s",
                        company_id, person_name, li_url,
                    )
                    s.contacts_added += 1
                elif not _contact_exists(conn, company_id, "linkedin", li_url):
                    _insert_contact(
                        conn,
                        company_id=company_id,
                        ctype="linkedin",
                        cvalue=li_url,
                        confidence=confidence,
                        sentinel=sentinel,
                    )
                    s.contacts_added += 1
                    log.info(
                        "CONTACT[linkedin]: company_id=%d %r %s conf=%.2f",
                        company_id, person_name, li_url, confidence,
                    )
                else:
                    log.info(
                        "SKIP_CONTACT[linkedin] (exists): company_id=%d %s",
                        company_id, li_url,
                    )
            if email:
                if no_write:
                    log.info(
                        "NO_WRITE contact[email]: company_id=%d %r %s",
                        company_id, person_name, email,
                    )
                    s.contacts_added += 1
                elif not _contact_exists(conn, company_id, "general_email", email):
                    _insert_contact(
                        conn,
                        company_id=company_id,
                        ctype="general_email",
                        cvalue=email,
                        confidence=confidence,
                        sentinel=sentinel,
                    )
                    s.contacts_added += 1
                    log.info(
                        "CONTACT[email]: company_id=%d %s conf=%.2f",
                        company_id, email, confidence,
                    )
                else:
                    log.info(
                        "SKIP_CONTACT[email] (exists): company_id=%d %s",
                        company_id, email,
                    )

    return s


# ── Run loop + budget ────────────────────────────────────────────────────────


def _preflight_estimate_usd(terms: list[str], max_per_term: int) -> float:
    return len(terms) * max_per_term * COST_PER_1K_LEADS_USD / 1000.0


def _print_preflight_plan(
    terms: list[str],
    max_results_per_term: int,
    *,
    no_budget_cap: bool,
    bypass_filter: bool,
    cum_cap: float,
) -> None:
    """Print the cost plan WITHOUT touching the network. Used by
    --preflight-only and exercised by the offline self-test.

    Intentionally pure-stdout / no I/O beyond print(): no actor client
    is constructed, no DB is opened. The whole point of this helper is
    that the caller can prove $0 spend by static inspection.
    """
    estimate = _preflight_estimate_usd(terms, max_results_per_term)
    per_term = _term_estimate_usd(max_results_per_term)
    print("=" * 70)
    print("apify_leads --preflight-only  ($0 spend; no actor call)")
    print("-" * 70)
    print(f"  terms ({len(terms)}):           {terms}")
    print(f"  max_results_per_term:  {max_results_per_term}")
    print(f"  cost / 1k leads:       ${COST_PER_1K_LEADS_USD:.2f}")
    print(f"  per-term worst-case:   ${per_term:.4f}")
    print(f"  total worst-case:      ${estimate:.4f}")
    print(f"  per-run preflight cap: ${DEFAULT_BUDGET_CAP_USD:.2f}")
    print(f"  cumulative cap:        ${cum_cap:.2f}"
          + ("  (DISABLED — --no-budget-cap set)" if no_budget_cap else ""))
    print(f"  bypass_filter:         {bypass_filter}")
    print("-" * 70)
    if no_budget_cap:
        print("  budget verdict:        all caps bypassed; full plan would run")
    elif estimate > DEFAULT_BUDGET_CAP_USD:
        print(f"  budget verdict:        WOULD ABORT — ${estimate:.4f} > "
              f"per-run cap ${DEFAULT_BUDGET_CAP_USD:.2f}")
    else:
        n_max_terms = int(cum_cap // per_term) if per_term > 0 else len(terms)
        n_will_run  = min(len(terms), n_max_terms)
        print(f"  budget verdict:        would run {n_will_run} of {len(terms)} term(s) "
              f"before cumulative cap fires")
    print("=" * 70)
    print("(no actor called; $0 spent; re-run without --preflight-only to execute)")


def _term_estimate_usd(max_per_term: int) -> float:
    """Worst-case spend for one term's actor call."""
    return max_per_term * COST_PER_1K_LEADS_USD / 1000.0


def _would_breach_cumulative_cap(
    cumulative_spend: float,
    term_estimate: float,
    cap_usd: float,
) -> bool:
    """Return True if running this next term would push spend over the cap.

    Standalone (no I/O) so the self-test can exercise it without the actor.
    Uses a sub-cent epsilon so that exact-equality cases (e.g. 5 × $0.17 vs.
    $0.85, which IEEE-754 evaluates to ~$0.85000000000000001) are NOT
    treated as breaches — we'd otherwise lose $0.17 of intended headroom
    to floating-point fuzz on every multi-term run.
    """
    epsilon_usd = 1e-6  # one-millionth of a dollar; well below cent resolution
    return (cumulative_spend + term_estimate) > (cap_usd + epsilon_usd)


def _cumulative_cap_self_test() -> int:
    """Simulate a 6-term run × $0.17/term against an $0.85 cap and verify
    the loop stops after term 5 (the would-be 6th call breaches the cap).

    No actor calls; this is pure arithmetic against
    `_would_breach_cumulative_cap`.
    """
    cap = 0.85
    per_term = 0.17                          # 100 leads × $1.70/1k
    n_terms = 6
    processed: list[int] = []
    cumulative = 0.0
    for i in range(1, n_terms + 1):
        if _would_breach_cumulative_cap(cumulative, per_term, cap):
            break
        cumulative += per_term
        processed.append(i)

    cases: list[tuple[str, bool]] = [
        ("processed exactly 5 terms",                  processed == [1, 2, 3, 4, 5]),
        ("cumulative spend == $0.85 after 5 terms",    abs(cumulative - 0.85) < 1e-9),
        ("term 6 was NOT processed",                   6 not in processed),
        ("breach detector True at $0.85 + $0.17",      _would_breach_cumulative_cap(0.85, 0.17, 0.85) is True),
        ("breach detector False at $0.00 + $0.17",     _would_breach_cumulative_cap(0.00, 0.17, 0.85) is False),
        ("breach detector True at $0.68 + $0.17 + ε",  _would_breach_cumulative_cap(0.68 + 1e-6, 0.17, 0.85) is True),
        ("--no-budget-cap simulated by skipping check passes 6/6",
         True if _would_breach_cumulative_cap(0.85, 0.17, float("inf")) is False else False),
    ]
    failures: list[str] = []
    for label, ok in cases:
        if ok:
            print(f"  PASS  {label}")
        else:
            failures.append(f"  FAIL  {label}")
            print(failures[-1])
    print(f"\nprocessed={processed}  cumulative_spend=${cumulative:.4f}")
    print(f"{len(cases) - len(failures)}/{len(cases)} cases passed")
    return 0 if not failures else 1


def _preflight_only_self_test() -> int:
    """Verify that --preflight-only path never constructs the Apify client.

    The proof is structural: `_print_preflight_plan` does its whole job
    with pure-Python arithmetic + print(). Any actor work lives inside
    `_run_actor` (which has a local `from apify_client import ApifyClient`).
    This test calls the preflight helper and snapshots `sys.modules` to
    show no apify_client import got triggered as a side effect.
    """
    import sys as _sys
    failures: list[str] = []

    apify_loaded_before = "apify_client" in _sys.modules

    # Capture stdout from the plan-print so we can assert the shape.
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_preflight_plan(
            ["counter-drone"], 100,
            no_budget_cap=False, bypass_filter=False, cum_cap=0.85,
        )
    out = buf.getvalue()
    apify_loaded_after = "apify_client" in _sys.modules

    cases: list[tuple[str, bool]] = [
        ("plan printed cost line",          "total worst-case:" in out),
        ("plan printed $0 disclaimer",      "$0 spent" in out),
        ("plan printed cap header",         "preflight-only" in out),
        ("no apify_client side-effect import",
                                            apify_loaded_after == apify_loaded_before),
        ("over-cap plan reports WOULD ABORT",
            "WOULD ABORT" in _capture_preflight(["t1","t2","t3","t4","t5","t6"], 100)),
        ("under-cap plan reports a positive 'would run' verdict",
            "would run" in _capture_preflight(["counter-drone"], 50)),
    ]
    for label, ok in cases:
        if ok:
            print(f"  PASS  {label}")
        else:
            failures.append(f"  FAIL  {label}")
            print(failures[-1])
    print(f"{len(cases) - len(failures)}/{len(cases)} preflight-only cases passed")
    return 0 if not failures else 1


def _capture_preflight(terms: list[str], max_per_term: int) -> str:
    """Test helper — return the printed plan as a string."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_preflight_plan(
            terms, max_per_term,
            no_budget_cap=False, bypass_filter=False, cum_cap=0.85,
        )
    return buf.getvalue()


def run(
    terms: list[str],
    max_results_per_term: int = 100,
    no_write: bool = False,
    no_budget_cap: bool = False,
    bypass_filter: bool = False,
    cumulative_budget_cap: float | None = None,
) -> int:
    """Per-term Apify run. Returns shell exit code (0 success, 2 budget abort).

    Two budget guards fire independently:
      - Per-run preflight (DEFAULT_BUDGET_CAP_USD): rejects runs whose
        worst-case total estimate exceeds the cap *before* any actor call.
      - Cumulative actual-spend (`cumulative_budget_cap`, defaulting to
        CUMULATIVE_BUDGET_CAP_USD): checked before each term — if running
        that term would push the running total over the cap, the run
        stops gracefully and returns the partial summary.
    `--no-budget-cap` bypasses BOTH (it disables every budget guard).
    """
    log_file = _setup_logging()
    cum_cap = (
        cumulative_budget_cap if cumulative_budget_cap is not None
        else CUMULATIVE_BUDGET_CAP_USD
    )
    log.info("=" * 70)
    log.info(
        "apify_leads start | terms=%d max_per_term=%d no_write=%s "
        "no_cap=%s bypass_filter=%s cum_cap=$%.2f",
        len(terms), max_results_per_term, no_write, no_budget_cap,
        bypass_filter, cum_cap,
    )
    log.info("log file: %s", log_file)

    # Build the baseline filter once. Skipped entirely if bypass_filter is
    # set (operator-driven backfill of pre-filter cohorts).
    baseline_filter: BaselineFilter | None = None
    if not bypass_filter:
        baseline_filter = BaselineFilter.from_yaml()
        log.info(
            "baseline filter loaded: %d rule(s) — %s",
            len(baseline_filter.rules),
            ", ".join(r.name for r in baseline_filter.rules),
        )
    else:
        log.warning("baseline filter BYPASSED — every org will reach store_lead")

    estimate = _preflight_estimate_usd(terms, max_results_per_term)
    log.info("preflight cost estimate (worst case): $%.2f for %d × %d leads",
             estimate, len(terms), max_results_per_term)

    if not no_budget_cap and estimate > DEFAULT_BUDGET_CAP_USD:
        msg = (
            f"BUDGET_ABORT: preflight estimate ${estimate:.2f} exceeds cap "
            f"${DEFAULT_BUDGET_CAP_USD:.2f}. Re-run with --no-budget-cap to override."
        )
        log.error(msg)
        return 2

    migrate_database(DB_PATH)

    # Build the canonical_name → company_id lookup once for the whole run.
    # On a per-term basis the DB doesn't change (we don't insert into
    # companies here — only raw_leads + founders/contacts), so this stays
    # correct across all terms.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    canonical_lookup = _build_canonical_name_lookup(conn)
    log.info("canonical_name lookup: %d entries", len(canonical_lookup))

    cumulative_leads = 0
    cumulative_spend = 0.0
    per_term_summary: list[dict] = []

    n_terms_total = len(terms)
    n_terms_processed = 0
    try:
        for term_idx, term in enumerate(terms, start=1):
            # Predictive cumulative guard: if running this term would push the
            # actual running total past the cumulative cap, stop *before*
            # paying for it. Uses worst-case per-term spend; the actor may
            # return fewer leads than max_results_per_term, but we plan for
            # the maximum so the cap can't be breached by a single term.
            term_estimate = _term_estimate_usd(max_results_per_term)
            if (
                not no_budget_cap
                and _would_breach_cumulative_cap(cumulative_spend, term_estimate, cum_cap)
            ):
                log.error(
                    "cumulative budget cap reached: $%.2f spent / $%.2f cap. "
                    "Stopping after %d of %d terms. Use --cumulative-budget-cap "
                    "or --no-budget-cap to override.",
                    cumulative_spend, cum_cap, n_terms_processed, n_terms_total,
                )
                break

            if no_write:
                items: list[dict] = []
                # --no-write still calls the actor (and still pays) — it's
                # the cheapest way to verify the parser against real Apollo
                # data. If you want a $0 plan-only run, use --preflight-only
                # which short-circuits before this loop.
                items = _run_actor(term, max_results_per_term)
            else:
                items = _run_actor(term, max_results_per_term)

            n_leads = len(items)
            term_spend = n_leads * COST_PER_1K_LEADS_USD / 1000.0
            cumulative_leads += n_leads
            cumulative_spend += term_spend

            grouped = _group_by_org(items)
            log.info("term=%r grouped %d leads → %d unique orgs", term, n_leads, len(grouped))

            t_stats = {
                "term":             term,
                "leads_fetched":    n_leads,
                "unique_orgs":      len(grouped),
                "orgs_skipped":     0,
                "orgs_rejected":    0,
                "orgs_new_to_db":   0,
                "founders_added":   0,
                "contacts_added":   0,
                "spend_usd":        round(term_spend, 4),
            }

            for bucket in grouped.values():
                s = _process_org(
                    bucket, conn, canonical_lookup,
                    no_write=no_write,
                    baseline_filter=baseline_filter,
                    bypass_filter=bypass_filter,
                )
                if s.skipped:
                    t_stats["orgs_skipped"] += 1
                if s.rejected:
                    t_stats["orgs_rejected"] += 1
                if s.new_lead:
                    t_stats["orgs_new_to_db"] += 1
                t_stats["founders_added"] += s.founders_added
                t_stats["contacts_added"] += s.contacts_added

            per_term_summary.append(t_stats)
            n_terms_processed = term_idx
            log.info(
                "term=%r summary: leads=%d unique_orgs=%d new=%d skipped=%d "
                "rejected=%d founders=%d contacts=%d spend=$%.4f cum_spend=$%.4f",
                term, n_leads, len(grouped), t_stats["orgs_new_to_db"],
                t_stats["orgs_skipped"], t_stats["orgs_rejected"],
                t_stats["founders_added"], t_stats["contacts_added"],
                term_spend, cumulative_spend,
            )
    finally:
        conn.close()

    # ── Summary ──
    log.info("\n" + "=" * 78)
    log.info("APIFY LEADS — run summary")
    log.info("─" * 78)
    log.info(f"{'term':<24} {'leads':>5} {'orgs':>5} {'new':>4} {'skip':>4} "
             f"{'rej':>4} {'fnd':>4} {'ct':>4}  {'spend':>8}")
    log.info("─" * 78)
    tot = {"leads": 0, "orgs": 0, "new": 0, "skip": 0, "rej": 0,
           "fnd": 0, "ct": 0, "spend": 0.0}
    for s in per_term_summary:
        log.info(
            f"{s['term'][:24]:<24} {s['leads_fetched']:>5} {s['unique_orgs']:>5} "
            f"{s['orgs_new_to_db']:>4} {s['orgs_skipped']:>4} "
            f"{s['orgs_rejected']:>4} {s['founders_added']:>4} "
            f"{s['contacts_added']:>4}  ${s['spend_usd']:>6.4f}"
        )
        tot["leads"]  += s["leads_fetched"]
        tot["orgs"]   += s["unique_orgs"]
        tot["new"]    += s["orgs_new_to_db"]
        tot["skip"]   += s["orgs_skipped"]
        tot["rej"]    += s["orgs_rejected"]
        tot["fnd"]    += s["founders_added"]
        tot["ct"]     += s["contacts_added"]
        tot["spend"]  += s["spend_usd"]
    log.info("─" * 78)
    log.info(
        f"{'TOTAL':<24} {tot['leads']:>5} {tot['orgs']:>5} {tot['new']:>4} "
        f"{tot['skip']:>4} {tot['rej']:>4} {tot['fnd']:>4} "
        f"{tot['ct']:>4}  ${tot['spend']:>6.4f}"
    )
    log.info("=" * 78)
    log.info(
        "cumulative spend: $%.4f / $%.2f cap  (terms processed: %d / %d)",
        cumulative_spend, cum_cap, n_terms_processed, n_terms_total,
    )
    if n_terms_processed < n_terms_total and not no_budget_cap:
        log.info(
            "partial run — %d term(s) skipped because of the cumulative cap",
            n_terms_total - n_terms_processed,
        )
    if no_write:
        log.info("(--no-write: nothing written to DB; actor calls above DID incur cost)")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Apify Leads collector (manual-run, cost-capped)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--terms",
        help="Comma-separated industry keywords, one Apify call per term. "
             "Required for live runs; ignored under --self-test.",
    )
    p.add_argument(
        "--max-results-per-term", type=int, default=100,
        help="maxResults sent to the actor per term (default 100, ≈ $0.17/term).",
    )
    p.add_argument(
        "--preflight-only", action="store_true",
        help="Print the cost plan (terms, max-results, estimated spend) and EXIT "
             "BEFORE any actor call. $0 spend. Always safe.",
    )
    p.add_argument(
        "--no-write", action="store_true",
        help="Suppress DB writes; the actor IS called and you DO pay. Use this "
             "to inspect real Apollo data without changing DB state. NOT a 'dry "
             "run' — for $0, use --preflight-only. (Replaces the old --dry-run "
             "flag, whose name implied $0 but did not deliver it.)",
    )
    p.add_argument(
        "--no-budget-cap", action="store_true",
        help=f"Bypass ALL budget guards (per-run preflight cap "
             f"${DEFAULT_BUDGET_CAP_USD:.2f} AND cumulative-spend cap "
             f"${CUMULATIVE_BUDGET_CAP_USD:.2f}).",
    )
    p.add_argument(
        "--cumulative-budget-cap", type=float, default=None, metavar="USD",
        help=f"Override the cumulative-spend cap (default "
             f"${CUMULATIVE_BUDGET_CAP_USD:.2f}). Fires before each term once "
             f"running total + that term's estimate would breach the cap. "
             f"Has no effect when --no-budget-cap is set.",
    )
    p.add_argument(
        "--bypass-filter", action="store_true",
        help="Skip the apify_baseline filter — write every org to raw_leads. "
             "Operator-driven backfill only; never use in scheduled runs.",
    )
    p.add_argument(
        "--self-test", action="store_true",
        help="Run cumulative-cap + preflight-only arithmetic self-tests "
             "(no actor calls) and exit.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.self_test:
        rc = _cumulative_cap_self_test()
        rc |= _preflight_only_self_test()
        sys.exit(rc)
    terms = [t.strip() for t in (args.terms or "").split(",") if t.strip()]
    if not terms:
        print("--terms is empty (required for live runs)", file=sys.stderr)
        sys.exit(2)
    if args.preflight_only:
        cum_cap = (
            args.cumulative_budget_cap
            if args.cumulative_budget_cap is not None
            else CUMULATIVE_BUDGET_CAP_USD
        )
        _print_preflight_plan(
            terms, args.max_results_per_term,
            no_budget_cap=args.no_budget_cap,
            bypass_filter=args.bypass_filter,
            cum_cap=cum_cap,
        )
        sys.exit(0)
    sys.exit(run(
        terms=terms,
        max_results_per_term=args.max_results_per_term,
        no_write=args.no_write,
        no_budget_cap=args.no_budget_cap,
        bypass_filter=args.bypass_filter,
        cumulative_budget_cap=args.cumulative_budget_cap,
    ))
