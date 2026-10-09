"""
Priority enrichment for the SBIR cohort.

Filters the 2,481 SBIR companies down to a high-signal subset:
  - At least one Phase II Department of Defense award
  - That award is from fiscal year 2022 or later
  - Total awards across SBIR-history is between 2 and 10
    (excludes contract mills and one-off grants)
  - Not yet enriched (companies.enriched_at IS NULL)

NOTE — deviation from the original spec: the spec used
"description IS NULL OR description = ''" as the unenriched proxy, but
SBIR-imported companies inherit their Award Title as their initial
description in promote.py, so that filter zeroed the cohort. The right
"hasn't been enriched yet" guard is enriched_at IS NULL — which is what
enrich.run itself uses.

Then drives enrich.run() with that explicit company_id list. enrich.run
already auto-runs score_one + classify_one per company on success, so
no separate scoring step is needed.

Usage:
    python -m src.enrich_priority_sbir
    python -m src.enrich_priority_sbir --limit 5
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# enrich.py expects to import sibling modules (score, classify, etc.) from
# the src/ directory directly. Mirror dashboard.py's bootstrap so this script
# works whether called as `python -m src.enrich_priority_sbir` or
# `python src/enrich_priority_sbir.py`.
_SRC = Path(__file__).parent
sys.path.insert(0, str(_SRC))

import enrich  # noqa: E402  — must follow sys.path mutation

DB_PATH = _SRC.parent / "data" / "companies.db"

PRIORITY_SQL = """
    SELECT DISTINCT c.id, c.name
      FROM companies c
      JOIN sbir_awards sa ON sa.company_id = c.id
     WHERE sa.phase = 'Phase II'
       AND sa.agency = 'Department of Defense'
       AND sa.fiscal_year >= 2022
       AND c.name IN (
           SELECT company_name
             FROM sbir_awards
            GROUP BY company_name
           HAVING COUNT(*) BETWEEN 2 AND 10
       )
       AND c.enriched_at IS NULL
     ORDER BY c.id
"""


def _select_priority_ids() -> list[tuple[int, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        return [(r[0], r[1]) for r in conn.execute(PRIORITY_SQL).fetchall()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max companies to enrich (testing).")
    parser.add_argument("--progress-every", type=int, default=10,
                        help="Log a progress line every N companies (default 10).")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    targets = _select_priority_ids()
    print(f"Priority cohort matches: {len(targets)} companies "
          f"(Phase II DOD, FY ≥ 2022, 2–10 total awards, no description yet)")
    if args.limit:
        targets = targets[:args.limit]
        print(f"  --limit applied → processing {len(targets)} of them")

    if not targets:
        print("Nothing to enrich. Exiting.")
        return

    ids = [t[0] for t in targets]
    print("First 5 in this batch:")
    for tid, tname in targets[:5]:
        print(f"  id={tid:>5}  {tname}")

    # enrich.run owns the actual loop, circuit breaker, scoring/classify hooks.
    # We just hand it the explicit ID list + a progress cadence.
    enrich.run(
        company_ids=ids,
        progress_every=args.progress_every,
        # limit applies inside enrich.run too, but we already sliced `ids`
        # so passing limit again would be redundant; leaving it None.
    )


if __name__ == "__main__":
    main()
