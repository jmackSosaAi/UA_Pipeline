"""Dry-run/ingest IQT portfolio discoveries with entity resolution.

Real writes are intentionally opt-in. Use ``--dry-run`` for review before any
database mutation.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.entity_resolution import (  # noqa: E402
    DEFAULT_DB_PATH,
    match_or_resolve_company,
    normalize_website,
)


def _sanitize_website(value: Any) -> str | None:
    """Return the website if it's a real URL (normalisable); else None.

    Defensive layer for ingest sources whose `website` field carries
    placeholder values like `"#"`, `""`, or `"N/A"`. Without this, the
    placeholder lands in the DB and trips `UNIQUE(companies.website)`
    on the second occurrence, then the conflict-resolver fallback
    silently merges unrelated companies (the Elemental Technologies
    incident, 2026-05-11)."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if not normalize_website(value):
        return None
    return value


INPUT_DEFAULT = ROOT / "docs" / "artifacts" / "discovery" / "iqt_portfolio" / "companies.json"
AMBIGUOUS_DEFAULT = ROOT / "docs" / "artifacts" / "discovery" / "iqt_portfolio" / "ambiguous_review.csv"
WEBSITE_CONFLICT_LOG = ROOT / "docs" / "artifacts" / "discovery" / "iqt_portfolio" / "website_conflicts.csv"


def _load_records(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}


def _json_array(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if item]


def _iqt_url(record: dict[str, Any]) -> str | None:
    return record.get("iqt_portfolio_page") or record.get("source_url")


def _first_category(record: dict[str, Any]) -> str | None:
    categories = record.get("categories") or []
    if not isinstance(categories, list):
        return None
    for category in categories:
        if category:
            return str(category)
    return None


def _candidate_field(candidates: list[dict[str, Any]], field: str) -> str:
    return "|".join(str(candidate.get(field, "")) for candidate in candidates)


def _is_ambiguous(diagnostic: dict[str, Any] | None) -> bool:
    if not diagnostic:
        return False
    return diagnostic.get("match_type") in {"ambiguous", "low_confidence"}


def _write_ambiguous(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["iqt_name", "candidate_db_ids", "candidate_db_names", "fuzzy_scores", "iqt_categories"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _append_source_url(existing_value: Any, url: str | None) -> str:
    urls = _json_array(existing_value)
    if url and url not in urls:
        urls.append(url)
    return json.dumps(urls, ensure_ascii=False)


def _update_existing(
    conn: sqlite3.Connection,
    columns: set[str],
    company_id: int,
    record: dict[str, Any],
) -> None:
    current = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if current is None:
        raise RuntimeError(f"company id {company_id} disappeared before update")
    updates: dict[str, Any] = {}
    raw_assignments: list[str] = []
    if "source_urls" in columns:
        updates["source_urls"] = _append_source_url(current["source_urls"], _iqt_url(record))
    if "mention_count" in columns:
        raw_assignments.append("mention_count = COALESCE(mention_count, 0) + 1")
    if "cross_validated" in columns:
        updates["cross_validated"] = 1
    if "website" in columns and not current["website"] and record.get("website"):
        updates["website"] = record["website"]
    if "primary_sector" in columns and not current["primary_sector"]:
        category = _first_category(record)
        if category:
            updates["primary_sector"] = category
    if not updates and not raw_assignments:
        return
    assignments = ", ".join([*(f"{column} = ?" for column in updates), *raw_assignments])
    conn.execute(
        f"UPDATE companies SET {assignments} WHERE id = ?",
        [*updates.values(), company_id],
    )


def _insert_new(conn: sqlite3.Connection, columns: set[str], record: dict[str, Any]) -> None:
    values: dict[str, Any] = {}
    if "name" in columns:
        values["name"] = record["name"]
    if "website" in columns:
        # Sanitize placeholder values (e.g. literal "#") to NULL so
        # they don't collide on the UNIQUE(website) constraint.
        values["website"] = _sanitize_website(record.get("website"))
    if "description" in columns:
        values["description"] = record.get("description")
    if "source" in columns:
        values["source"] = "iqt_portfolio"
    if "source_urls" in columns:
        values["source_urls"] = _append_source_url(None, _iqt_url(record))
    if "mention_count" in columns:
        values["mention_count"] = 1
    if "primary_sector" in columns:
        values["primary_sector"] = _first_category(record)
    if "hq_country" in columns:
        values["hq_country"] = record.get("country")
    if "cross_validated" in columns:
        values["cross_validated"] = 0
    if not values:
        raise RuntimeError("companies table has no supported insert columns")
    placeholders = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO companies ({', '.join(values)}) VALUES ({placeholders})",
        list(values.values()),
    )


def _log_website_conflict(rows: list[dict[str, Any]]) -> None:
    """Append website-conflict diagnostics for ops visibility."""
    if not rows:
        return
    WEBSITE_CONFLICT_LOG.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["iqt_name", "iqt_website", "existing_company_id", "existing_name", "error_class"]
    write_header = not WEBSITE_CONFLICT_LOG.exists()
    with WEBSITE_CONFLICT_LOG.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _resolve_website_conflict(
    conn: sqlite3.Connection, columns: set[str], record: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    """Defense-in-depth: when _insert_new trips the website UNIQUE
    constraint, look up the existing company by exact website and
    return (existing_id, diag) so we can merge instead of insert.
    Returns None if no exact-website match exists (then re-raise).

    SAFETY GUARD: rejects placeholder/non-website values (e.g. literal
    "#") so a "#" UNIQUE collision does NOT silently merge unrelated
    companies. If the value doesn't normalise to a real URL, this
    returns None so the caller re-raises the IntegrityError instead
    of attempting a bad merge. Added 2026-05-11 after the Elemental
    Technologies incident."""
    website = record.get("website")
    if not website or not normalize_website(website):
        return None
    row = conn.execute(
        "SELECT id, name FROM companies WHERE website = ?", (website,),
    ).fetchone()
    if row is None:
        return None
    return row["id"], {"id": row["id"], "name": row["name"]}


def run(args: argparse.Namespace) -> dict[str, int]:
    records = _load_records(args.input)
    if args.limit:
        records = records[: args.limit]

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    columns = _columns(conn)

    summary = {"input": 0, "matched": 0, "ambiguous": 0, "new": 0, "skipped": 0,
               "website_conflict_recovered": 0, "fill_missing_skipped": 0}
    ambiguous_rows: list[dict[str, Any]] = []
    website_conflicts: list[dict[str, Any]] = []
    try:
        for record in records:
            summary["input"] += 1
            name = (record.get("name") or "").strip()
            if not name:
                summary["skipped"] += 1
                print("SKIP missing-name")
                continue
            company_id, diagnostic = match_or_resolve_company(
                name, country=None, linkedin_url=None,
                website=record.get("website"), db_path=args.db,
            )
            match_type = (diagnostic or {}).get("match_type")
            if company_id is not None:
                if args.fill_missing_only:
                    # Recovery mode: don't touch already-represented
                    # rows. Skip without merging to avoid double-
                    # incrementing mention_count or re-appending source
                    # URLs that are already present.
                    summary["fill_missing_skipped"] += 1
                    print(f"SKIP fill-missing-only company_id={company_id} "
                          f"name={name!r} match_type={match_type}")
                    continue
                summary["matched"] += 1
                print(f"MERGE company_id={company_id} name={name!r} match_type={match_type}")
                if not args.dry_run:
                    _update_existing(conn, columns, company_id, record)
                continue
            if _is_ambiguous(diagnostic):
                summary["ambiguous"] += 1
                candidates = (diagnostic or {}).get("candidates", [])
                print(f"AMBIGUOUS name={name!r} candidates={_candidate_field(candidates, 'id')}")
                ambiguous_rows.append(
                    {
                        "iqt_name": name,
                        "candidate_db_ids": _candidate_field(candidates, "id"),
                        "candidate_db_names": _candidate_field(candidates, "name"),
                        "fuzzy_scores": _candidate_field(candidates, "score"),
                        "iqt_categories": "|".join(str(category) for category in record.get("categories", []) if category),
                    }
                )
                continue
            summary["new"] += 1
            print(f"INSERT name={name!r}")
            if not args.dry_run:
                try:
                    _insert_new(conn, columns, record)
                except sqlite3.IntegrityError as e:
                    # Defense-in-depth: website UNIQUE constraint may
                    # still fire when the resolver missed (race, edge
                    # case, malformed website on existing row). Log the
                    # conflict and try a website-keyed merge instead.
                    err = str(e).lower()
                    if "unique constraint failed: companies.website" not in err:
                        raise
                    resolved = _resolve_website_conflict(conn, columns, record)
                    if resolved is None:
                        # No exact-website match exists; the constraint
                        # tripped for a different reason. Re-raise.
                        raise
                    existing_id, info = resolved
                    print(f"  ↑ recovered: website conflict — merging into "
                          f"company_id={existing_id} ({info['name']!r})")
                    summary["new"] -= 1
                    summary["matched"] += 1
                    summary["website_conflict_recovered"] += 1
                    website_conflicts.append({
                        "iqt_name": name,
                        "iqt_website": record.get("website") or "",
                        "existing_company_id": existing_id,
                        "existing_name": info["name"],
                        "error_class": "UNIQUE_constraint_website",
                    })
                    _update_existing(conn, columns, existing_id, record)
        if ambiguous_rows:
            _write_ambiguous(args.ambiguous_output, ambiguous_rows)
        elif args.ambiguous_output.exists() and args.dry_run:
            _write_ambiguous(args.ambiguous_output, [])
        if website_conflicts and not args.dry_run:
            _log_website_conflict(website_conflicts)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
            # D-021: reconcile portfolio_company markers against the
            # portfolio table after IQT inserts. The IQT cohort
            # includes companies that may already be in the fund's portfolio
            # under a slightly different name (e.g. an IQT-portfolio
            # row that's also independently in portfolio).
            try:
                import sys as _sys
                if str(ROOT / "src") not in _sys.path:
                    _sys.path.insert(0, str(ROOT / "src"))
                from ingest import mark_portfolio_companies  # noqa: E402
                n_marked = mark_portfolio_companies(conn)
                if n_marked:
                    print(f"portfolio_reconciliation: {n_marked} newly-marked row(s)")
            except Exception as e:
                print(f"portfolio_reconciliation skipped: {e}")
    finally:
        conn.close()
    print(
        "SUMMARY "
        f"input={summary['input']} matched={summary['matched']} "
        f"ambiguous={summary['ambiguous']} new={summary['new']} skipped={summary['skipped']} "
        f"website_conflict_recovered={summary['website_conflict_recovered']} "
        f"fill_missing_skipped={summary['fill_missing_skipped']} "
        f"fill_missing_only={args.fill_missing_only} "
        f"dry_run={args.dry_run}"
    )
    if ambiguous_rows:
        print(f"ambiguous_review={args.ambiguous_output}")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest IQT portfolio discoveries with entity resolution.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--input", type=Path, default=INPUT_DEFAULT)
    parser.add_argument("--ambiguous-output", type=Path, default=AMBIGUOUS_DEFAULT)
    parser.add_argument("--dry-run", action="store_true", help="Print decisions without writing companies.")
    parser.add_argument(
        "--fill-missing-only", action="store_true",
        help="Insert only IQT rows that are NOT yet represented in "
             "the DB (no name or website match). Skip everything else "
             "without merging — used for recovery after a partial "
             "ingest, where re-running the full path would double-"
             "increment mention_count on already-merged rows.",
    )
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        print("WARNING: real ingest mode will write to the companies table.", file=sys.stderr)
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
