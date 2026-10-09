"""
Re-score and re-classify companies whose enrichment data is newer than their last score.

Finds companies WHERE enriched_at > scored_at OR scored_at IS NULL.

Run:
    python src/rescore.py
    python src/rescore.py --dry-run
    python -m src.rescore
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# Ensure src/ is in sys.path regardless of invocation style (python src/rescore.py vs -m src.rescore)
_SRC = Path(__file__).parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import score as _score
import classify as _classify
from db.migrate import migrate as migrate_database

DB_PATH = _SRC.parent / "data" / "companies.db"


def rescore_stale(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """
    Find enriched companies with no score or a score older than their enriched_at,
    then re-score and re-classify each one.
    Returns the count of companies processed.
    """
    _score._migrate_db(conn)
    _classify._migrate_db(conn)

    rows = conn.execute(
        """SELECT id, name, enriched_at, scored_at
             FROM companies
            WHERE enriched_at IS NOT NULL
              AND enrich_error IS NULL
              AND (portfolio_company IS NULL OR portfolio_company = 0)
              AND (status IS NULL OR status != 'duplicate')
              AND (scored_at IS NULL OR enriched_at > scored_at)
            ORDER BY name"""
    ).fetchall()

    if not rows:
        print("No stale scores — all enriched companies are up to date.")
        return 0

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Found {len(rows)} companies with stale or missing scores:\n")

    count = 0
    for row in rows:
        cid   = row["id"]
        name  = row["name"]
        ea    = (row["enriched_at"] or "")[:16]
        sa    = (row["scored_at"]   or "never")[:16]
        print(f"  [{cid:4d}] {name:<40}  enriched={ea}  scored={sa}")

        if not dry_run:
            result = _score.score_one(cid, conn)
            primary, _ = _classify.classify_one(cid, conn)
            if result:
                tier_mark = (
                    "★" if result["total_score"] >= 2.2 else
                    "◆" if result["total_score"] >= 1.5 else "·"
                )
                print(
                    f"         {tier_mark} score={result['total_score']:.2f}"
                    f"  category={primary}"
                )
        count += 1

    if dry_run:
        print(f"\n  (no changes written — re-run without --dry-run to apply)")
    else:
        print(f"\nRe-scored {count} companies.")

    return count


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Re-score companies with enrichment data newer than their last score.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Show which companies would be re-scored without writing anything.",
    )
    args = ap.parse_args()

    migrate_database(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rescore_stale(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
