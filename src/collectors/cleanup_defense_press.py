"""
One-shot cleanup of the existing 105 source='Defense Press' companies rows.

CATEGORIZATION REFERENCE — manual classification done while writing this
script (used to verify the gate's outputs make sense). The cleanup applies
no manual list; it reuses defense_press.is_defense_relevant() so the script
behaves identically to future ingest runs.

DEFENSE — should pass the gate (~70 rows, e.g.):
  ASELSAN, Acecore Technologies, AeroVironment, Inc., Airvolute,
  American Rheinmetall, Baykar, Bell, Benchmark Space Systems,
  Beyond Vision, Blue Storm Associates, BlueHalo, CoAspire, Damen Naval,
  Dark Wolf Solutions, DeltaQuad, Elbit, Firestorm Labs, Frankenburg,
  GE Aerospace, General Atomics, General Dynamics Electric Boat,
  General Dynamics NASSCO, Gibbs and Cox, Gurzuf Defence, HII,
  Height Technologies, Highcat, Intelic, Kaman Corporation, Kratos,
  Latent AI, Leonardo DRS, Lockheed, Meteksan, Meteksan Defence,
  Moog Inc., Near Earth Autonomy, Neros, Octopus, Origin Robotics,
  Oshkosh, Patria, Pemdas Technologies, Pratt & Whitney, Quantum Space,
  Rafael Advanced Defense Systems, Revolution Space, Robinson Helicopter
  Company, Robinson Unmanned, Roketsan, Rolls-Royce, SNC, Saildrone,
  Shield AI, Sikorsky, SkyFall, Skycutter, Summa Defence, Tekever,
  Terradepth, Thales Netherlands, Tytan, Wilcox Industries, Wild Hornets,
  Williams International, XP Services

NOISE — should be filtered (~33 rows):
  Media:           ABC News, Axios, Bloomberg News, Fox News
  Sponsor blurbs:  Adobe, Splunk, Peraton, Voyager
  Publisher/site:  Government Media Executive Group LLC, Forecast
                   International, GovTribe, OilPrice.com, Recurrent,
                   UNIX LLC
  Think-tanks:     Center for Naval Analyses, Center for Strategic and
                   Budgetary Assessments, Defense Priorities, Mitchell
                   Institute for Aerospace Studies, NCSIST, RAND,
                   US Geospatial Intelligence Foundation
  Consulting:      The Artemis Group
  Industry assoc:  UCDI
  Oil/shipping/    ADNOC, Hapag-Lloyd, Kpler, Naftogaz Group,
  trade-data       National Iranian Oil Company,
                   National Iranian Tanker Company,
                   Planet Labs, TankerTrackers.com Inc., Windward
  Sanctioned:      Agro-Fregat
  Tech-mega-cap    Anthropic
  Not-a-company:   Iron Fist (system, not a vendor)

AMBIGUOUS:
  Atlantic Models    — model maker for Boeing hypersonic display.
  Bombardier         — civilian airframer used by E-11A; not investable.

The gate is intentionally narrow ("media outlet", "publisher",
"news network", "news organization", "sponsor", "sponsored by",
"research firm", "consulting firm"), so descriptions that don't match a
noise pattern AND mention any defense vocab term will pass — including
the oil/shipping/trade-data bucket above. Those rows survive this
cleanup and would need a richer rule set if we wanted them retired too.

Constraints:
- Does NOT run schema migrations. relevance_filter column already exists.
- Does NOT run VACUUM.
- Each row's work is wrapped in its own small transaction so locks don't
  linger while a concurrent enrichment job writes to companies.
- Idempotent: re-runs do nothing the second time around.
- article_companies mention rows are preserved (re-pointed when a duplicate
  press row is deleted, never dropped).

Usage:
  python -m src.collectors.cleanup_defense_press --dry-run
  python -m src.collectors.cleanup_defense_press
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .base import DB_PATH
from .dedup import normalize_name
from .defense_press import is_defense_relevant

FILTERED_VALUE = "filtered_out_defense_press_noise"


def _ac_link_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM article_companies").fetchone()[0]


def _process_row(
    conn: sqlite3.Connection,
    press_id: int,
    name: str,
    description: str | None,
    relevance_filter: str | None,
    dry_run: bool,
    counters: dict,
) -> str:
    """Run the cleanup decision for one Defense Press row.

    Each call uses its own short transaction. Returns the action taken so
    the caller can format a per-row line.
    """
    canon_name = normalize_name(name)

    # Idempotency: if we already filtered this row in a previous run, skip.
    if relevance_filter == FILTERED_VALUE:
        counters["already_filtered"] += 1
        return "already_filtered"

    # ── Duplicate path ────────────────────────────────────────────────────
    canonical = conn.execute(
        "SELECT id, company_id FROM canonical_companies WHERE canonical_name = ?",
        (canon_name,),
    ).fetchone()

    if canonical is not None and canonical[1] is not None and canonical[1] != press_id:
        target_company_id = canonical[1]
        # Count links being repointed for the summary; don't actually run UPDATE
        # if dry-run.
        n_links = conn.execute(
            "SELECT COUNT(*) FROM article_companies WHERE company_id = ?",
            (press_id,),
        ).fetchone()[0]

        if not dry_run:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "UPDATE article_companies SET company_id = ? WHERE company_id = ?",
                    (target_company_id, press_id),
                )
                conn.execute("DELETE FROM companies WHERE id = ?", (press_id,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        counters["merged"] += 1
        counters["mention_links_repointed"] += n_links
        return f"merge → company_id={target_company_id} (repointed {n_links} link(s))"

    # ── Relevance-gate path ───────────────────────────────────────────────
    if is_defense_relevant(name, description):
        counters["kept"] += 1
        return "kept"

    # Negative gate — mark filtered_out.
    if not dry_run:
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE companies SET relevance_filter = ? WHERE id = ?",
                (FILTERED_VALUE, press_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    counters["filtered"] += 1
    return "filtered"


def run(dry_run: bool, db_path: Path = DB_PATH) -> int:
    counters = {
        "kept":                    0,
        "filtered":                0,
        "merged":                  0,
        "mention_links_repointed": 0,
        "already_filtered":        0,
    }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ac_count_before = _ac_link_count(conn)
        rows = conn.execute(
            "SELECT id, name, description, relevance_filter "
            "FROM companies WHERE source = 'Defense Press' ORDER BY name"
        ).fetchall()
        if not rows:
            print("No Defense Press rows in companies. Nothing to do.")
            return 0

        prefix = "[DRY RUN] " if dry_run else ""
        print(f"{prefix}Processing {len(rows)} Defense Press rows...\n")

        for r in rows:
            action = _process_row(
                conn,
                press_id=r["id"],
                name=r["name"],
                description=r["description"],
                relevance_filter=r["relevance_filter"],
                dry_run=dry_run,
                counters=counters,
            )
            tag = {
                "kept":              "  KEEP   ",
                "filtered":          "  FILTER ",
                "merged":            "  MERGE  ",
                "already_filtered":  "  SKIP   ",
            }.get(action.split(" →")[0], "  ?      ")
            # Truncate name + description for the per-row output.
            desc_preview = (r["description"] or "")[:90]
            print(f"{tag}id={r['id']:>5}  {r['name'][:36]!r:38s}  {action}")

        ac_count_after = _ac_link_count(conn)
    finally:
        conn.close()

    # ── Summary ──
    print("\n" + "=" * 70)
    print(f"{'[DRY RUN] ' if dry_run else ''}Summary")
    print(f"  kept                     : {counters['kept']:>4}")
    print(f"  filtered                 : {counters['filtered']:>4}  → relevance_filter='{FILTERED_VALUE}'")
    print(f"  merged (duplicate path)  : {counters['merged']:>4}")
    print(f"  mention links re-pointed : {counters['mention_links_repointed']:>4}")
    print(f"  already filtered (skip)  : {counters['already_filtered']:>4}")
    print()
    print(f"  article_companies before : {ac_count_before:>4}")
    print(f"  article_companies after  : {ac_count_after:>4}  "
          f"(must be ≥ before; deletion of a duplicate companies row preserves links via UPDATE)")
    print("=" * 70)
    if dry_run:
        print("Dry run only — no rows were modified.")
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would change; do not write to the DB.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(run(dry_run=args.dry_run))
