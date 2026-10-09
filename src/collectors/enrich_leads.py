"""
Enrichment bridge: raw_leads → enrichment pipeline.

Identifies canonical companies that have unenriched raw leads and selects
the best available lead data to drive the enrichment pass.

One enrichment run per canonical_id — never once per raw_lead row.

Run:
    python -m src.collectors.enrich_leads --dry-run
    python -m src.collectors.enrich_leads
"""

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "companies.db"

# Sources ranked by authoritativeness for picking the "best" lead when a
# canonical has multiple raw leads.  Lower number = higher priority.
_SOURCE_PRIORITY: dict[str, int] = {
    "NATO DIANA 2026": 1,
    "Brave1":          2,
    "Manual":          3,
    "LinkedIn":        4,
    "Conference":      5,
    "Referral":        6,
}
_DEFAULT_PRIORITY = 99


def _source_rank(source: str) -> int:
    return _SOURCE_PRIORITY.get(source, _DEFAULT_PRIORITY)


# ---------------------------------------------------------------------------
# Query logic
# ---------------------------------------------------------------------------


def get_pending_canonicals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Return canonical companies that have no enriched raw lead yet.

    A canonical is considered enriched when at least one of its raw_leads
    has enriched = 1.  This ensures we run exactly once per canonical_id,
    not once per raw_lead row.
    """
    return conn.execute(
        """
        SELECT cc.id AS canonical_id, cc.canonical_name
          FROM canonical_companies cc
         WHERE cc.id NOT IN (
               SELECT DISTINCT canonical_id
                 FROM raw_leads
                WHERE enriched = 1
                  AND canonical_id IS NOT NULL
               )
         ORDER BY cc.canonical_name
        """
    ).fetchall()


def get_best_lead(canonical_id: int, conn: sqlite3.Connection) -> sqlite3.Row | None:
    """
    For a given canonical_id, return the single raw_lead that best represents
    the company for enrichment purposes.

    Selection priority (in order):
      1. Has an initial_description (longest wins)
      2. Source authoritativeness (NATO DIANA 2026 > Brave1 > Manual > …)
      3. Most recently discovered (discovered_at DESC)
    """
    rows = conn.execute(
        """
        SELECT id, company_name, source, source_url,
               initial_description, category_hint, country, discovered_at,
               website, source_metadata
          FROM raw_leads
         WHERE canonical_id = ?
         ORDER BY
               length(COALESCE(initial_description, '')) DESC,
               discovered_at DESC
        """,
        (canonical_id,),
    ).fetchall()

    if not rows:
        return None

    # Re-sort in Python to apply source priority as a tiebreaker without
    # a complex CASE expression in SQL.
    sorted_rows = sorted(
        rows,
        key=lambda r: (
            -(len(r["initial_description"] or "")),  # longer description first
            _source_rank(r["source"]),               # authoritative source first
        ),
    )
    return sorted_rows[0]


# ---------------------------------------------------------------------------
# Main enrichment runner
# ---------------------------------------------------------------------------


def run(dry_run: bool = False) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    pending = get_pending_canonicals(conn)

    if not pending:
        print("All canonical companies are already enriched — nothing to do.")
        conn.close()
        return

    print(f"{'[DRY RUN] ' if dry_run else ''}Canonical companies pending enrichment: {len(pending)}")

    if dry_run:
        print()
        for row in pending:
            lead = get_best_lead(row["canonical_id"], conn)
            desc_preview = ""
            if lead and lead["initial_description"]:
                desc_preview = f"  →  {lead['initial_description'][:80]}"
            source_label = f"[{lead['source']}]" if lead else "[no lead]"
            print(
                f"  {row['canonical_id']:4d}  {row['canonical_name']:<45}  "
                f"{source_label}{desc_preview}"
            )
        conn.close()
        return

    # ---------------------------------------------------------------------------
    # TODO: wire to full enrichment pipeline
    #
    # For each canonical, call the enrichment step with the best lead's data:
    #
    #   for row in pending:
    #       lead = get_best_lead(row["canonical_id"], conn)
    #       if lead is None:
    #           continue
    #
    #       # Run enrichment (web search + Claude extraction)
    #       result = enrich_company(
    #           company_name=lead["company_name"],
    #           source_url=lead["source_url"],
    #           category_hint=lead["category_hint"],
    #           country=lead["country"],
    #       )
    #
    #       # Mark all raw_leads for this canonical as enriched
    #       conn.execute(
    #           "UPDATE raw_leads SET enriched = 1, enriched_at = datetime('now')"
    #           " WHERE canonical_id = ?",
    #           (row["canonical_id"],),
    #       )
    #       conn.commit()
    # ---------------------------------------------------------------------------

    print("Full enrichment not yet implemented — run with --dry-run to preview.")
    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich canonical companies from raw_leads (one pass per canonical)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print pending companies without running enrichment.",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
