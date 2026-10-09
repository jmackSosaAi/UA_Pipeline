"""Bring previously-rejected orgs back into the pipeline.

Reads `rejected_orgs.raw_metadata` for each given id and replays the
write that the baseline filter blocked: `store_lead(...)` with the same
fields and source. Marks the row `unrejected_at = now()` and stores the
new raw_leads.id in `unrejected_to_lead_id` so a later audit can see
which rejections were eventually accepted.

Use this after editing config/sources/apify_baseline.yaml to loosen a
rule, or after the operator decides on a previously-Open question and
some rejections turn out to be in-thesis.

Usage:
    python scripts/unreject.py --id 12 13 14
    python scripts/unreject.py --id 12 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from collectors.base import DB_PATH, store_lead  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Re-inject rejected orgs as raw_leads.")
    p.add_argument("--id", type=int, nargs="+", required=True,
                   help="One or more rejected_orgs.id values.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be re-injected without writing.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    placeholders = ",".join("?" for _ in args.id)
    rows = conn.execute(
        f"SELECT id, company_name, source, rejection_reasons, raw_metadata, "
        f"       unrejected_at, unrejected_to_lead_id "
        f"FROM rejected_orgs WHERE id IN ({placeholders})",
        list(args.id),
    ).fetchall()

    found_ids = {r["id"] for r in rows}
    missing = [i for i in args.id if i not in found_ids]
    if missing:
        print(f"WARN: rejected_orgs ids not found: {missing}", file=sys.stderr)

    n_done = 0
    for r in rows:
        if r["unrejected_at"]:
            print(
                f"  SKIP id={r['id']} {r['company_name']!r} — already unrejected at "
                f"{r['unrejected_at']} (lead_id={r['unrejected_to_lead_id']})"
            )
            continue

        try:
            meta = json.loads(r["raw_metadata"]) if r["raw_metadata"] else {}
        except (TypeError, ValueError):
            meta = {}

        # Reconstruct the store_lead args. Apify metadata bucket layout:
        #   meta = {industry, founded_year, size, city, state, country,
        #           linkedin, specialities, description, website?}
        country     = meta.get("country")
        website     = meta.get("website")  # Some rows store website in meta;
        # for apify_leads we also stored org_website at the top level of the
        # bucket — fall back to the dedicated key if present.
        if not website:
            website = meta.get("org_website")
        description = (meta.get("description") or "")[:500] or None

        if args.dry_run:
            print(
                f"  DRY id={r['id']} would re-inject {r['company_name']!r} "
                f"source={r['source']} country={country!r} website={website!r}"
            )
            continue

        new_lead_id = store_lead(
            company_name=r["company_name"],
            source=r["source"],
            source_url=None,
            initial_description=description,
            country=country,
            website=website or None,
            source_metadata=meta,
        )
        if new_lead_id is None:
            print(
                f"  DUP  id={r['id']} {r['company_name']!r} — raw_leads "
                f"({r['source']}, {r['company_name']!r}) already exists; "
                f"marking unrejected without a new lead_id."
            )
        else:
            print(
                f"  OK   id={r['id']} {r['company_name']!r} → raw_leads.id={new_lead_id}"
            )

        conn.execute(
            "UPDATE rejected_orgs SET unrejected_at = datetime('now'), "
            "unrejected_to_lead_id = ? WHERE id = ?",
            (new_lead_id, r["id"]),
        )
        conn.commit()
        n_done += 1

    print(f"\nunrejected {n_done} of {len(rows)} candidate row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
