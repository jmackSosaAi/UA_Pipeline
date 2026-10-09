"""Search the rejected_orgs table by keyword / source.

Read-only — never mutates the table. Use this after a paid Apify run to
sanity-check that the baseline filter isn't dropping orgs you actually
want. If you find a false negative, run scripts/unreject.py with the
returned id(s) to bring those rows back into the pipeline.

Usage:
    python scripts/search_rejections.py --keyword counter-uas
    python scripts/search_rejections.py --keyword anduril --source apify_leads
    python scripts/search_rejections.py --keyword robotics --limit 100
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from collectors.base import DB_PATH  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Search rejected_orgs.")
    p.add_argument("--keyword", required=True,
                   help="Case-insensitive substring matched against "
                        "company_name, rejection_reasons, and raw_metadata.")
    p.add_argument("--source", default=None,
                   help="Restrict to a single source tag (e.g. 'apify_leads').")
    p.add_argument("--limit", type=int, default=50,
                   help="Max rows to return (default 50).")
    p.add_argument("--include-unrejected", action="store_true",
                   help="Include rows that were already brought back into the pipeline.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    pat = f"%{args.keyword}%"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    where = [
        "(company_name LIKE ? COLLATE NOCASE "
        "OR rejection_reasons LIKE ? COLLATE NOCASE "
        "OR raw_metadata LIKE ? COLLATE NOCASE)"
    ]
    params: list = [pat, pat, pat]
    if args.source:
        where.append("source = ?")
        params.append(args.source)
    if not args.include_unrejected:
        where.append("unrejected_at IS NULL")
    params.append(args.limit)

    sql = (
        "SELECT id, company_name, source, rejected_at, rejection_reasons, raw_metadata, "
        "       unrejected_at, unrejected_to_lead_id "
        "FROM rejected_orgs "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY rejected_at DESC LIMIT ?"
    )

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print(f"no rejections match keyword={args.keyword!r} source={args.source!r}")
        return 0

    print(f"matched {len(rows)} rejection(s):\n")
    for r in rows:
        try:
            reasons = json.loads(r["rejection_reasons"])
        except (TypeError, ValueError):
            reasons = [r["rejection_reasons"]]
        try:
            meta = json.loads(r["raw_metadata"]) if r["raw_metadata"] else {}
        except (TypeError, ValueError):
            meta = {}

        flags = []
        if r["unrejected_at"]:
            flags.append(f"UNREJECTED at {r['unrejected_at']} (lead_id={r['unrejected_to_lead_id']})")
        flag_str = "  [" + ", ".join(flags) + "]" if flags else ""

        print(f"  id={r['id']:<5} {r['company_name']!r:<40} source={r['source']}{flag_str}")
        print(f"    rejected_at: {r['rejected_at']}")
        print(f"    reasons:     {reasons}")
        meta_summary = {
            k: meta.get(k)
            for k in ("country", "industry", "size", "founded_year", "city", "state")
            if meta.get(k)
        }
        if meta_summary:
            print(f"    org_meta:    {meta_summary}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
