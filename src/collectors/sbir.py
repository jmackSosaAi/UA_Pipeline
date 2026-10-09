"""
SBIR/STTR collector — streams the local SBIR.gov bulk CSV.

We previously hit https://api.www.sbir.gov but that endpoint is
rate-limited (10 req / 10 min) and was down for maintenance. The bulk
CSV (~350 MB) at data/sbir/award_data.csv contains the same award rows
without any of those constraints.

Two-tier filter (config in config/sources/sbir_config.yaml):

  core_agencies      → standard search_vocabulary keyword filter only
                       (DOD, DHS — already defense)
  dual_use_agencies  → standard vocab AND at least one dual_use_keyword
                       (NASA, DOE — skip pure civilian work)

Each award that passes is stored as a raw lead via store_lead() and
recorded in sbir_awards (one row per Agency Tracking Number).

Run:
    python -m src.collectors.sbir
    python -m src.collectors.sbir --since-year 2024 --limit 100
    python -m src.collectors.sbir --agency "Department of Defense" --dry-run --verbose
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

try:
    from db.migrate import migrate as migrate_database
except ImportError:  # supports `python -m src.collectors.sbir`
    from src.db.migrate import migrate as migrate_database

from .base import DB_PATH, store_lead
from .dedup import find_exact_match, normalize_name
from .source_config import load_sbir_config
from .vocabulary import get_all_sector_keys, get_sector_terms, load_vocabulary

SOURCE = "SBIR"

DEFAULT_CSV = Path(__file__).parent.parent.parent / "data" / "sbir" / "award_data.csv"
DEFAULT_SINCE_YEARS_BACK = 5     # 5 years back from today
PROGRESS_EVERY = 1000

# CSV uses Latin-1-ish encoding; some abstracts contain mangled bytes.
# We open with utf-8 + errors='replace' so a single bad row never aborts the run.
_CSV_ENCODING = "utf-8"
_CSV_ENCODING_ERRORS = "replace"


# ── Vocabulary + filter ───────────────────────────────────────────────────────


def _build_vocab_terms() -> list[str]:
    """All sector terms (primary + search) + general modifiers, lowercased."""
    terms: set[str] = set()
    for key in get_all_sector_keys():
        for t in get_sector_terms(key):
            terms.add(t.strip().lower())
    for t in load_vocabulary().get("general_modifiers", []):
        terms.add(t.strip().lower())
    return sorted(terms, key=len, reverse=True)


def _row_search_text(row: dict) -> str:
    parts = [row.get("Award Title") or "", row.get("Topic Code") or "", row.get("Abstract") or ""]
    return " ".join(p for p in parts if p)


def _passes_filters(
    row: dict,
    vocab_terms: list[str],
    dual_use_terms: list[str] | None,
) -> tuple[bool, int, int]:
    """Returns (passed, vocab_hits, dual_use_hits)."""
    text = _row_search_text(row).lower()
    if not text:
        return False, 0, 0
    vocab_hits = sum(1 for t in vocab_terms if t and t in text)
    if vocab_hits == 0:
        return False, 0, 0
    if dual_use_terms is None:
        return True, vocab_hits, 0
    dual_hits = sum(1 for t in dual_use_terms if t and t in text)
    if dual_hits == 0:
        return False, vocab_hits, 0
    return True, vocab_hits, dual_hits


# ── CSV row → DB types ────────────────────────────────────────────────────────


def _parse_amount(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        return float(raw.replace(",", "").replace("$", "").strip())
    except ValueError:
        return 0.0


def _parse_year(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _parse_award_date(raw: str | None) -> str | None:
    """Convert MM/DD/YYYY (or M/D/YYYY) → 'YYYY-MM-DD'. Returns None if unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _award_key(row: dict) -> str:
    """Stable identifier for sbir_awards.award_number."""
    for k in ("Agency Tracking Number", "Contract"):
        v = (row.get(k) or "").strip()
        if v:
            return v
    firm = (row.get("Company") or "").strip().lower().replace(" ", "_")
    yr = (row.get("Award Year") or "").strip()
    tc = (row.get("Topic Code") or "").strip()
    return f"synth-{firm}-{yr}-{tc}"


def _source_url(company_name: str) -> str:
    return f"https://www.sbir.gov/awards?firm={quote_plus(company_name)}"


# ── DB write ──────────────────────────────────────────────────────────────────


def _store_award(
    conn: sqlite3.Connection,
    row: dict,
) -> tuple[int, int, bool]:
    """Insert one CSV row into sbir_awards + raw_leads.

    Returns (inserted_award, duplicate_award, new_lead).
    Skips silently if Company name is missing.
    """
    firm = (row.get("Company") or "").strip()
    if not firm:
        return 0, 0, False

    award_number = _award_key(row)

    if conn.execute(
        "SELECT 1 FROM sbir_awards WHERE award_number = ? LIMIT 1", (award_number,)
    ).fetchone():
        return 0, 1, False

    new_lead_id = store_lead(
        company_name=firm,
        source=SOURCE,
        source_url=_source_url(firm),
        initial_description=(row.get("Award Title") or "")[:500] or None,
        country=None,
        website=(row.get("Company Website") or "").strip() or None,
        source_metadata={
            "agency":     row.get("Agency"),
            "branch":     row.get("Branch"),
            "phase":      row.get("Phase"),
            "program":    row.get("Program"),
            "award_year": row.get("Award Year"),
            "amount":     row.get("Award Amount"),
            "topic_code": row.get("Topic Code"),
            "city":       row.get("City"),
            "state":      row.get("State"),
        },
    )

    canonical_id = find_exact_match(normalize_name(firm), conn)

    conn.execute(
        """
        INSERT OR IGNORE INTO sbir_awards
            (award_number, company_id, canonical_id, company_name,
             agency, branch, program, phase, amount, topic_code,
             topic_title, abstract, awarded_at, fiscal_year, source_url)
        VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            award_number, canonical_id, firm,
            row.get("Agency"),
            row.get("Branch"),
            row.get("Program"),
            row.get("Phase"),
            _parse_amount(row.get("Award Amount")),
            row.get("Topic Code"),
            row.get("Award Title"),
            row.get("Abstract"),
            _parse_award_date(row.get("Proposal Award Date")),
            _parse_year(row.get("Award Year")),
            _source_url(firm),
        ),
    )
    conn.commit()
    return 1, 0, bool(new_lead_id)


def backfill_company_ids() -> int:
    """Fill sbir_awards.company_id by joining canonical → companies on name.

    Same shape as defense_press.backfill_company_ids — only updates rows
    where company_id IS NULL and canonical_id IS NOT NULL.
    """
    with sqlite3.connect(DB_PATH) as conn:
        comp_by_canon: dict[str, int] = {}
        for cid, cname in conn.execute("SELECT id, name FROM companies").fetchall():
            if not cname:
                continue
            comp_by_canon.setdefault(normalize_name(cname), cid)

        rows = conn.execute(
            """
            SELECT sa.id, cc.canonical_name
              FROM sbir_awards sa
              JOIN canonical_companies cc ON cc.id = sa.canonical_id
             WHERE sa.company_id IS NULL AND sa.canonical_id IS NOT NULL
            """
        ).fetchall()

        updated = 0
        for ac_id, canon_name in rows:
            comp_id = comp_by_canon.get(canon_name)
            if comp_id is not None:
                conn.execute(
                    "UPDATE sbir_awards SET company_id = ? WHERE id = ?",
                    (comp_id, ac_id),
                )
                updated += 1
        conn.commit()
    return updated


# ── Main ──────────────────────────────────────────────────────────────────────


def run(
    csv_path: Path,
    since_year: int,
    agency_filter: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    cfg = load_sbir_config()
    core_set = set(cfg.get("core_agencies", []))
    dual_set = set(cfg.get("dual_use_agencies", []))
    dual_kw = [k.lower() for k in cfg.get("dual_use_keywords", [])]
    eligible = core_set | dual_set

    if agency_filter:
        if agency_filter not in eligible:
            print(f"Unknown agency: {agency_filter!r}", file=sys.stderr)
            print(f"Valid: {sorted(eligible)}", file=sys.stderr)
            sys.exit(2)
        eligible = {agency_filter}
        core_set = core_set & eligible
        dual_set = dual_set & eligible

    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(2)

    migrate_database(DB_PATH)

    vocab_terms = _build_vocab_terms()
    print(f"CSV:           {csv_path}")
    print(f"since_year:    {since_year}")
    print(f"agency_filter: {agency_filter or '—'}")
    print(f"vocab terms:   {len(vocab_terms)}")
    print(f"dual-use kws:  {len(dual_kw)}")
    print(f"core agencies: {sorted(core_set)}")
    print(f"dual agencies: {sorted(dual_set)}")
    print(f"dry_run:       {dry_run}")
    print()

    scanned = kept = year_skipped = agency_skipped = vocab_skipped = dual_skipped = 0
    inserted = duplicate_award = new_lead = 0
    by_agency: dict[str, int] = {}
    by_phase: dict[str, int] = {}

    with open(csv_path, encoding=_CSV_ENCODING, errors=_CSV_ENCODING_ERRORS, newline="") as f, \
         sqlite3.connect(DB_PATH) as conn:
        reader = csv.DictReader(f)
        for row in reader:
            scanned += 1

            if limit is not None and scanned > limit:
                scanned -= 1   # don't count the row that triggered the limit
                break

            if scanned % PROGRESS_EVERY == 0:
                print(
                    f"  …processed {scanned:,}  kept {kept:,}  skipped "
                    f"{year_skipped + agency_skipped + vocab_skipped + dual_skipped:,}"
                )

            year = _parse_year(row.get("Award Year"))
            if year is None or year < since_year:
                year_skipped += 1
                continue

            agency = (row.get("Agency") or "").strip()
            if agency not in eligible:
                agency_skipped += 1
                continue

            terms_for_dual = dual_kw if agency in dual_set else None
            passed, vh, dh = _passes_filters(row, vocab_terms, terms_for_dual)
            if not passed:
                if vh == 0:
                    vocab_skipped += 1
                else:
                    dual_skipped += 1
                if verbose:
                    print(
                        f"    [skip] {agency[:30]} | vh={vh} dh={dh} | "
                        f"{(row.get('Company') or '')[:30]} | "
                        f"{(row.get('Award Title') or '')[:60]}"
                    )
                continue

            if verbose:
                print(
                    f"    [PASS] {agency[:30]} | vh={vh} dh={dh} | "
                    f"{row.get('Company')} | {(row.get('Award Title') or '')[:60]}"
                )

            kept += 1
            by_agency[agency] = by_agency.get(agency, 0) + 1
            phase = (row.get("Phase") or "—").strip() or "—"
            by_phase[phase] = by_phase.get(phase, 0) + 1

            if not dry_run:
                ins, dup, nl = _store_award(conn, row)
                inserted += ins
                duplicate_award += dup
                if nl:
                    new_lead += 1

    if not dry_run:
        n_back = backfill_company_ids()
    else:
        n_back = 0

    # ── Summary ──
    print("\n" + "=" * 70)
    print(f"Total scanned:        {scanned:,}")
    print(f"  year-skipped:       {year_skipped:,}")
    print(f"  agency-skipped:     {agency_skipped:,}")
    print(f"  vocab-skipped:      {vocab_skipped:,}")
    print(f"  dual-use-skipped:   {dual_skipped:,}")
    print(f"  KEPT:               {kept:,}")
    if not dry_run:
        print(f"\nDB writes:")
        print(f"  awards inserted:    {inserted:,}")
        print(f"  duplicate awards:   {duplicate_award:,}")
        print(f"  new raw_leads:      {new_lead:,}")
        print(f"  backfilled co_ids:  {n_back:,}")
    else:
        print("\n(--dry-run: no DB writes)")

    if by_agency:
        print(f"\nBy agency:")
        for ag, n in sorted(by_agency.items(), key=lambda x: -x[1]):
            print(f"  {ag:48s} {n:>6,}")
    if by_phase:
        print(f"\nBy phase:")
        for ph, n in sorted(by_phase.items(), key=lambda x: -x[1]):
            print(f"  {ph:12s} {n:>6,}")
    print("=" * 70)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SBIR/STTR collector (local CSV)")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                   help=f"Path to bulk CSV (default {DEFAULT_CSV})")
    p.add_argument("--since-year", type=int,
                   default=datetime.now().year - DEFAULT_SINCE_YEARS_BACK,
                   help="Earliest Award Year to keep (default: 5 years back from today).")
    p.add_argument("--agency", type=str, default=None,
                   help="Process only this agency (full name from sbir_config.yaml).")
    p.add_argument("--limit", type=int, default=None,
                   help="Max rows to scan (testing).")
    p.add_argument("--dry-run", action="store_true",
                   help="Filter and report without writing to DB.")
    p.add_argument("--verbose", action="store_true",
                   help="Log each row decision (PASS/skip).")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(
        csv_path=args.csv,
        since_year=args.since_year,
        agency_filter=args.agency,
        limit=args.limit,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
