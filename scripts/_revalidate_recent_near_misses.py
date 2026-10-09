"""One-shot — re-validate the 17 NEAR_MISS rows from the SBIR verification batch.

The SBIR verification batch (commit `4fa4bb1`) produced 17 NEAR_MISS
rows where D-014's NULL-country branch deferred verification. The
SBIR country backfill (this commit) just populated `hq_country =
'United States'` for those rows. Re-running the matcher with the
now-populated country flips them to:
  - MATCH if harvestapi's returned country agrees with US
  - NEAR_MISS via D-008's mismatch path if harvestapi returns
    something else (a genuine cross-border name collision, the
    canon-v2 win we couldn't measure pre-backfill)

Audit semantics: writes NEW rows to `linkedin_url_discoveries`
rather than modifying the old NEAR_MISS rows. The audit trail
shows the verdict evolved as we corrected the underlying data.

Cost: 17 × $0.004 = $0.068.

Usage:
    python scripts/_revalidate_recent_near_misses.py
    python scripts/_revalidate_recent_near_misses.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(dotenv_path=ROOT / ".env")

from collectors.base import DB_PATH                            # noqa: E402
from collectors.linkedin_canonicalize import (                  # noqa: E402
    classify_match_with_country,
    country_from_description,
    _call_harvestapi,
    _index_results_by_url,
)
from collectors.apify_linkedin_url_discovery import (           # noqa: E402
    record_discovery,
)

WINDOW_MINUTES = 180   # the SBIR batch ran ~1h ago; widen to capture safely
SBIR_TAG = "SBIR"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Print plan; no actor call, no DB write.")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Pull the SBIR NEAR_MISS rows from the last batch window.
    cohort = conn.execute(f"""
        SELECT d.company_id, d.attempted_slug, d.attempted_url,
               c.name, c.hq_country
        FROM linkedin_url_discoveries d
        JOIN companies c ON c.id = d.company_id
        WHERE d.outcome = 'NEAR_MISS'
          AND d.attempted_at > datetime('now', '-{WINDOW_MINUTES} minutes')
          AND c.source = ?
        ORDER BY d.id
    """, (SBIR_TAG,)).fetchall()
    print(f"re-validation cohort: {len(cohort)} SBIR NEAR_MISS row(s) "
          f"from last {WINDOW_MINUTES} min")
    if not cohort:
        print("nothing to re-validate.")
        return 0

    est = len(cohort) * 0.004
    print(f"  estimated spend: ${est:.4f}")
    if args.dry_run:
        for r in cohort:
            print(f"  id={r['company_id']:<5} name={(r['name'] or '')[:30]:<30} "
                  f"hq_country={r['hq_country']!r:<14} url={r['attempted_url']}")
        print(f"\nDRY RUN — no actor call, no DB write.")
        return 0

    # One batched harvestapi call.
    urls = [r["attempted_url"] for r in cohort]
    items = _call_harvestapi(urls)
    spent = len(items) * 0.004
    idx = _index_results_by_url(items)

    counts = {"MATCH": 0, "NEAR_MISS": 0, "COLLISION": 0,
              "STALE_SLUG": 0, "NOT_FOUND": 0}
    n_writes = 0
    for row in cohort:
        key = (row["attempted_url"] or "").rstrip("/").lower()
        it = idx.get(key)
        if it is None or (it.get("error") and "not found" in it["error"].lower()):
            outcome = "STALE_SLUG"
            counts[outcome] += 1
            record_discovery(
                conn,
                company_id=row["company_id"],
                attempted_slug=row["attempted_slug"],
                attempted_url=row["attempted_url"],
                outcome=outcome,
                candidate_source="revalidate_after_country_backfill",
            )
            continue
        returned_name = it.get("name")
        returned_country = (it.get("locations") or [{}])[0].get("country") \
            or country_from_description(it.get("description"))
        cls, score, reason = classify_match_with_country(
            row["name"] or "", returned_name or "",
            row["hq_country"], returned_country,
        )
        counts[cls] = counts.get(cls, 0) + 1
        record_discovery(
            conn,
            company_id=row["company_id"],
            attempted_slug=row["attempted_slug"],
            attempted_url=row["attempted_url"],
            outcome=cls,
            returned_org_name=returned_name,
            fuzzy_score=round(score, 1),
            candidate_source="revalidate_after_country_backfill",
        )
        if cls == "MATCH":
            conn.execute(
                "UPDATE companies SET linkedin_url = ? WHERE id = ?",
                (row["attempted_url"], row["company_id"]),
            )
            n_writes += 1
    conn.commit()

    print("\n=== re-validation summary ===")
    for k in ("MATCH", "NEAR_MISS", "COLLISION", "STALE_SLUG"):
        print(f"  {k:<11} {counts.get(k, 0)}")
    print(f"  linkedin_url writes: {n_writes}")
    print(f"  spend:               ${spent:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
