"""One-time SBIR `hq_country` backfill — 'United States' as structural default.

The US federal SBIR program is open only to small businesses that
qualify as US small concerns under 13 CFR § 121.702. Encoding
`source='SBIR' → hq_country='United States'` is therefore a statutory
fact about the program, not an inferred default.

This backfill exists because the SBIR collector historically left
`hq_country` NULL on import. The Phase 2a SBIR verification batch
(commit `4fa4bb1`) hit a 6% MATCH abort because the canon-v2 matcher
(D-014) correctly deferred all 17 NEAR_MISS rows that had NULL
country — country verification was impossible. Backfilling structurally
unblocks discovery without lowering D-014's protection.

Scope:
  - Auto-fills SBIR rows where hq_country IS NULL or empty.
  - Does NOT auto-modify rows whose hq_country is non-NULL but
    something other than 'United States'. Those go in a WARN list
    for operator review:
      - 'USA' synonyms should arguably be normalised, but doing so
        belongs in a separate normalisation pass — out of scope here.
      - Genuinely non-US values (Canada, Ukraine, Sweden, China, …)
        could be data-quality issues or legitimate exceptions
        (extremely unusual under SBIR rules). Operator decides.

Idempotent: re-running results in 0 rows updated. Safe to execute
multiple times.

See `docs/DECISIONS.md` D-015 for the rationale.

Usage:
    python scripts/_backfill_sbir_country.py --dry-run
    python scripts/_backfill_sbir_country.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from collectors.base import DB_PATH                            # noqa: E402

US_NAME = "United States"


def report(conn: sqlite3.Connection) -> tuple[int, list[sqlite3.Row]]:
    """Return (backfill_target_count, warn_rows)."""
    n_target = conn.execute(
        "SELECT COUNT(*) FROM companies "
        "WHERE source='SBIR' AND (hq_country IS NULL OR TRIM(hq_country) = '')"
    ).fetchone()[0]
    warn_rows = conn.execute(
        "SELECT id, name, hq_country FROM companies "
        "WHERE source='SBIR' "
        "  AND hq_country IS NOT NULL AND TRIM(hq_country) != '' "
        f"  AND hq_country != ? "
        "ORDER BY id",
        (US_NAME,),
    ).fetchall()
    return n_target, warn_rows


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Print diagnostics + would-be rowcount; no DB write.")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    n_target, warn = report(conn)
    print(f"SBIR backfill scope:")
    print(f"  rows with NULL/empty hq_country (auto-fill):    {n_target}")
    print(f"  rows with non-NULL, non-'United States' (WARN): {len(warn)}")
    if warn:
        print("\nWARN cohort (operator review — NOT auto-corrected):")
        print(f"  {'id':<5} {'hq_country':<14} name")
        for r in warn:
            print(f"  {r['id']:<5} {(r['hq_country'] or '')[:14]:<14} {r['name']}")
        print()
        print("  Most are likely 'USA' synonyms (should be normalised in a "
              "separate pass).")
        print("  The genuinely non-US ones (Canada / Ukraine / Sweden / "
              "China / …) need")
        print("  operator triage — could be data-quality issues OR "
              "legitimate exceptions under")
        print("  SBIR's foreign-owned-subsidiary rules.")
    print()

    if args.dry_run:
        print(f"DRY RUN — would UPDATE companies SET hq_country = '{US_NAME}' "
              f"affecting {n_target} row(s); no write executed.")
        return 0

    cur = conn.execute(
        "UPDATE companies SET hq_country = ? "
        "WHERE source='SBIR' AND (hq_country IS NULL OR TRIM(hq_country) = '')",
        (US_NAME,),
    )
    rowcount = cur.rowcount
    conn.commit()
    print(f"LIVE: UPDATE affected {rowcount} row(s).")

    # Idempotency proof
    n_target_after, _ = report(conn)
    print(f"post-update NULL count: {n_target_after}  (expect 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
