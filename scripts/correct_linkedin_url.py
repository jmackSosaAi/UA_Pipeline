"""Operator helper — confirm or nullify a flagged linkedin_url row.

Used after `linkedin_canonicalize --validate-all` flags a row as
NEAR_MISS (left in place pending review). The operator inspects the
data/linkedin_url_review_*.md report, decides per row, and runs:

    python scripts/correct_linkedin_url.py --id 306 --action nullify --note "ZA collision"
    python scripts/correct_linkedin_url.py --id 749 --action confirm --note "name + country match"

Either action stamps an audit row in linkedin_url_corrections so the
review report stays reproducible. `confirm` is recorded with
correction_type='OPERATOR_CONFIRMED' so it doesn't pollute the
collision/stale-slug counts. `nullify` writes correction_type='COLLISION'
(or 'STALE_SLUG' if --as-stale) and NULLs companies.linkedin_url.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from collectors.base import DB_PATH  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Manually correct a flagged linkedin_url row.")
    p.add_argument("--id", type=int, required=True,
                   help="companies.id to act on.")
    p.add_argument("--action", required=True, choices=["confirm", "nullify"],
                   help="confirm = leave URL alone, audit-log the decision; "
                        "nullify = clear linkedin_url and audit-log.")
    p.add_argument("--note", required=True,
                   help="Operator note (stored in returned_org_name for traceability).")
    p.add_argument("--as-stale", action="store_true",
                   help="When --action nullify, record correction_type='STALE_SLUG' "
                        "instead of 'COLLISION' (use when the URL was outright dead).")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT id, name, linkedin_url FROM companies WHERE id = ?", (args.id,)
    ).fetchone()
    if row is None:
        print(f"  ERROR: companies.id={args.id} not found", file=sys.stderr)
        return 2

    old_url = row["linkedin_url"]
    print(f"  company: id={row['id']}  name={row['name']!r}")
    print(f"  current linkedin_url: {old_url!r}")

    if args.action == "confirm":
        ctype = "OPERATOR_CONFIRMED"
        if old_url is None:
            print("  WARN: linkedin_url is already NULL — confirming a non-existent URL.",
                  file=sys.stderr)
        conn.execute(
            """
            INSERT INTO linkedin_url_corrections
                (company_id, old_linkedin_url, returned_org_name,
                 correction_type, fuzzy_score)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (args.id, old_url or "", f"OPERATOR NOTE: {args.note}", ctype),
        )
        conn.commit()
        print(f"  OK   audit-logged 'OPERATOR_CONFIRMED' for id={args.id}; URL untouched.")
        return 0

    # nullify
    ctype = "STALE_SLUG" if args.as_stale else "COLLISION"
    conn.execute(
        """
        INSERT INTO linkedin_url_corrections
            (company_id, old_linkedin_url, returned_org_name,
             correction_type, fuzzy_score)
        VALUES (?, ?, ?, ?, NULL)
        """,
        (args.id, old_url or "", f"OPERATOR NOTE: {args.note}", ctype),
    )
    conn.execute(
        "UPDATE companies SET linkedin_url = NULL WHERE id = ?",
        (args.id,),
    )
    conn.commit()
    print(f"  OK   nullified companies.linkedin_url for id={args.id} (type={ctype}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
