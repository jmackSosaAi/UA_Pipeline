"""
Promotion step: canonical_companies → companies table.

Moves each canonical company that doesn't already exist in the companies table
into it, so the existing enrich / score / classify pipeline can process it.

One row inserted per canonical_id.  Existing companies (matched by normalized
name) are recognised and skipped — no duplicates created.

Run:
    python -m src.collectors.promote
    python -m src.collectors.promote --dry-run
    python -m src.collectors.promote --limit 20
    python -m src.collectors.promote --dry-run --db /tmp/promote_smoke.db

NOTE: enrich.py processes all unenriched companies. Website discovery happens
inside run() — no separate website-finder step is needed.
"""

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

try:
    from db.migrate import migrate as migrate_database
except ImportError:  # supports `python -m src.collectors.promote`
    from src.db.migrate import migrate as migrate_database

from .dedup import normalize_name
from .enrich_leads import get_best_lead

DB_PATH = Path(__file__).parent.parent.parent / "data" / "companies.db"

# ---------------------------------------------------------------------------
# Category mapping — DIANA challenge areas → companies.primary_category values.
# Checked at runtime against actual DB values; falls back to the raw string.
# ---------------------------------------------------------------------------
_DIANA_CATEGORY_MAP: dict[str, str] = {
    "Advanced Communications":              "Communications / EW",
    "Autonomy and Unmanned Systems":        "UAV / UAS",
    "Contested Electromagnetic Spectrum":   "Electronic Warfare",
    "Critical Infrastructure and Logistics": "C2 / Battle Management",
    "Data and Decision Making":             "Military AI & Software",
    "Energy and Power":                     "Energy & Power",
    "Human Resilience and Biotechnology":   "Human Performance / MedTech",
    "Maritime Operations":                  "Maritime / Naval",
    "Extreme Environments":                 "Extreme Environments",
    "Resilient Space Operations":           "Space & Satellite",
    "General":                              None,
}


def _get_existing_categories(conn: sqlite3.Connection) -> set[str]:
    """Return all distinct primary_category values currently in companies."""
    rows = conn.execute(
        "SELECT DISTINCT primary_category FROM companies WHERE primary_category IS NOT NULL"
    ).fetchall()
    return {r[0] for r in rows}


def _map_category(category_hint: str | None, existing: set[str]) -> str | None:
    """Map a raw category_hint to the closest existing primary_category, or use as-is."""
    if not category_hint:
        return None
    mapped = _DIANA_CATEGORY_MAP.get(category_hint, category_hint)
    if mapped and mapped in existing:
        return mapped
    # If the mapped value isn't a known category, fall back to the raw hint
    if category_hint in existing:
        return category_hint
    return mapped  # use it as a new category — classify.py will overwrite it anyway


def _build_companies_index(conn: sqlite3.Connection) -> dict[str, int]:
    """Return {normalized_name: company_id} for every row in companies."""
    rows = conn.execute("SELECT id, name FROM companies").fetchall()
    index: dict[str, int] = {}
    for cid, name in rows:
        norm = normalize_name(name)
        if norm not in index:          # first occurrence wins on collision
            index[norm] = cid
    return index


def _get_all_canonicals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all canonical companies, alphabetically."""
    return conn.execute(
        """
        SELECT id AS canonical_id, canonical_name, company_id
          FROM canonical_companies
         ORDER BY canonical_name
        """
    ).fetchall()


def _get_company_by_id(company_id: int | None, conn: sqlite3.Connection) -> sqlite3.Row | None:
    if company_id is None:
        return None
    return conn.execute(
        "SELECT id, name FROM companies WHERE id = ? LIMIT 1",
        (company_id,),
    ).fetchone()


def _link_canonical(canonical_id: int, company_id: int, conn: sqlite3.Connection) -> None:
    """Persist canonical_companies -> companies linkage for idempotent promotion."""
    conn.execute(
        "UPDATE canonical_companies SET company_id = ? WHERE id = ?",
        (company_id, canonical_id),
    )
    conn.commit()


def _mark_promoted(canonical_id: int, conn: sqlite3.Connection) -> None:
    """Mark raw_leads as promoted using legacy enriched/enriched_at columns."""
    conn.execute(
        "UPDATE raw_leads SET enriched = 1, enriched_at = datetime('now') WHERE canonical_id = ?",
        (canonical_id,),
    )
    conn.commit()


def _ensure_canonical_company_id_col(conn: sqlite3.Connection) -> None:
    """Idempotent guard: add canonical_companies.company_id if migrate() hasn't.

    Lets promote.py run safely on an older DB without forcing a separate
    migration step.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(canonical_companies)")}
    if "company_id" not in cols:
        conn.execute(
            "ALTER TABLE canonical_companies ADD COLUMN company_id INTEGER REFERENCES companies(id)"
        )
        conn.commit()


def _insert_company(
    lead: sqlite3.Row,
    category: str | None,
    canonical_id: int,
    conn: sqlite3.Connection,
) -> tuple[int | None, str]:
    """Insert one row into companies.

    Returns (company_id, status) where status is one of:
      - "new"     — fresh row inserted; company_id is the new id
      - "merged"  — website collided with an existing row; company_id is
                    the existing row's id, canonical_companies updated to
                    point at it
      - "failed"  — IntegrityError fired but we couldn't locate the
                    matching existing company (shouldn't normally happen)
    """
    try:
        cursor = conn.execute(
            """
            INSERT INTO companies (
                name, website, source, description, hq_country, primary_category,
                status, status_updated_at, added_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'sourced', datetime('now'), datetime('now'))
            """,
            (
                lead["company_name"],
                lead["website"],
                lead["source"],
                lead["initial_description"],
                lead["country"],
                category,
            ),
        )
        conn.commit()
        return cursor.lastrowid, "new"
    except sqlite3.IntegrityError:
        # The only UNIQUE constraint that bites here is companies.website.
        # Find the existing company sharing that website and link this
        # canonical to it.
        website = lead["website"]
        if not website:
            return None, "failed"
        existing = conn.execute(
            "SELECT id, name FROM companies WHERE website = ? LIMIT 1",
            (website,),
        ).fetchone()
        if existing is None:
            return None, "failed"
        existing_id = existing["id"]
        _link_canonical(canonical_id, existing_id, conn)
        return existing_id, "merged"


def _apply_source_metadata(company_id: int, source: str, meta: dict, conn: sqlite3.Connection) -> None:
    """Apply source-specific fields from source_metadata JSON to the companies row."""
    if source == "prozorro":
        conn.execute(
            """UPDATE companies SET
                   edrpou            = COALESCE(edrpou, ?),
                   name_latin        = COALESCE(name_latin, ?),
                   procurement_value = COALESCE(procurement_value, 0.0) + ?,
                   procuring_entity  = ?,
                   tender_count      = COALESCE(tender_count, 0) + ?
               WHERE id = ?""",
            (
                meta.get("edrpou"),
                meta.get("name_latin"),
                meta.get("procurement_value", 0.0),
                meta.get("procuring_entity"),
                meta.get("tender_count", 1),
                company_id,
            ),
        )
    elif source == "brave1_articles":
        conn.execute(
            """UPDATE companies SET
                   funding_amount = COALESCE(funding_amount, ?),
                   investors      = COALESCE(investors, ?),
                   source_urls    = ?,
                   mention_count  = COALESCE(mention_count, 0) + ?
               WHERE id = ?""",
            (
                meta.get("funding_amount"),
                json.dumps(meta.get("investors") or []),
                json.dumps(meta.get("source_urls") or []),
                meta.get("mention_count", 1),
                company_id,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Core promote logic
# ---------------------------------------------------------------------------

def run(
    dry_run: bool = False,
    limit: int | None = None,
    db_path: str | Path = DB_PATH,
) -> None:
    active_db = Path(db_path)
    migrate_database(active_db)
    conn = sqlite3.connect(active_db)
    conn.row_factory = sqlite3.Row

    _ensure_canonical_company_id_col(conn)

    canonicals     = _get_all_canonicals(conn)
    companies_idx  = _build_companies_index(conn)
    existing_cats  = _get_existing_categories(conn)

    if limit:
        canonicals = canonicals[:limit]

    promoted = 0
    merged = 0
    failed = 0
    already_linked = 0
    already_existed = 0
    no_lead = 0

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Using DB: {active_db}")
    print(f"{prefix}Processing {len(canonicals)} canonical companies...\n")

    for row in canonicals:
        canonical_id   = row["canonical_id"]
        canonical_name = row["canonical_name"]
        canonical_company_id = row["company_id"]

        linked_company = _get_company_by_id(canonical_company_id, conn)
        if linked_company is not None:
            print(
                f"  Already promoted: {canonical_name!r:50s}  "
                f"(canonical linked to companies.id={linked_company['id']})"
            )
            if not dry_run:
                _mark_promoted(canonical_id, conn)
            already_linked += 1
            continue

        # Check whether this canonical already has a match in companies.
        # companies_idx is keyed by normalized name, not the raw canonical string.
        existing_id = companies_idx.get(normalize_name(canonical_name))
        if existing_id is not None:
            print(
                f"  Already in companies: {canonical_name!r:50s}  "
                f"(companies.id={existing_id})"
            )
            if not dry_run:
                _link_canonical(canonical_id, existing_id, conn)
                _mark_promoted(canonical_id, conn)
            already_existed += 1
            continue

        # Get best raw_lead for this canonical
        lead = get_best_lead(canonical_id, conn)
        if lead is None:
            print(f"  No lead found:  {canonical_name!r:50s}  — skipping")
            no_lead += 1
            continue

        category = _map_category(lead["category_hint"], existing_cats)

        if dry_run:
            desc_preview = (lead["initial_description"] or "")[:60]
            print(
                f"  Would promote:  {lead['company_name']!r:50s}  "
                f"[{lead['source']}]  cat={category!r}"
                + (f"\n{'':18s}desc: {desc_preview}" if desc_preview else "")
            )
            promoted += 1
            continue

        new_id, status = _insert_company(lead, category, canonical_id, conn)

        if status == "new":
            if new_id is None:
                print(
                    f"  [skip] Insert reported success for {lead['company_name']!r} "
                    f"but did not return a company id",
                    file=sys.stderr,
                )
                failed += 1
                continue
            _link_canonical(canonical_id, new_id, conn)
            if lead["source_metadata"]:
                try:
                    meta = json.loads(lead["source_metadata"])
                    _apply_source_metadata(new_id, lead["source"], meta, conn)
                except (json.JSONDecodeError, TypeError):
                    pass
            companies_idx[normalize_name(canonical_name)] = new_id
            _mark_promoted(canonical_id, conn)
            print(
                f"  Promoted: {lead['company_name']!r:50s}  "
                f"[{lead['source']}]  → companies.id={new_id}"
            )
            promoted += 1
        elif status == "merged":
            companies_idx[normalize_name(canonical_name)] = new_id
            _mark_promoted(canonical_id, conn)
            print(
                f"  Merged canonical {canonical_name!r} → existing company "
                f"id={new_id} (matched on website)"
            )
            merged += 1
        else:  # status == "failed"
            print(
                f"  [skip] IntegrityError on {lead['company_name']!r} but no "
                f"existing row found by website {lead['website']!r}",
                file=sys.stderr,
            )
            failed += 1

    conn.close()

    total_conn = sqlite3.connect(active_db)
    total_in_companies = total_conn.execute(
        "SELECT COUNT(*) FROM companies"
    ).fetchone()[0]
    total_conn.close()

    print(f"\n{'=' * 60}")
    print(f"{prefix}Summary")
    print(f"  {'Would promote' if dry_run else 'Promoted'}  : {promoted}")
    if not dry_run:
        print(f"  Merged          : {merged}   (website collision → linked to existing company)")
        if failed:
            print(f"  Failed          : {failed}   (IntegrityError without recoverable match)")
    print(f"  Already promoted: {already_linked}   (canonical already linked to companies)")
    print(f"  Already in companies: {already_existed}   (matched by normalized name)")
    print(f"  No lead found   : {no_lead}")
    print(f"  Total in companies table: {total_in_companies}")
    if dry_run:
        print(f"  (no company/link/promotion changes written — re-run without --dry-run to apply)")
    print("=" * 60)

    if not dry_run and promoted:
        print(
            "\nNOTE: Promoted companies are loaded by Deal Flow, but may be hidden "
            "by default if they have no description, enrichment, or dossier. "
            "Enable ‘Show unenriched companies’ or run enrichment next."
        )


def _assert_count(conn: sqlite3.Connection, sql: str, expected: int, label: str) -> None:
    actual = conn.execute(sql).fetchone()[0]
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected}, got {actual}")


def _run_temp_smoke_test() -> None:
    """Exercise promotion idempotency against a temporary SQLite DB only."""
    tmp = tempfile.NamedTemporaryFile(
        prefix="ua_promote_smoke_",
        suffix=".db",
        dir="/tmp",
        delete=False,
    )
    db_path = Path(tmp.name)
    tmp.close()

    migrate_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    canonical_name = normalize_name("Smoke Test Robotics")
    cur = conn.execute(
        "INSERT INTO canonical_companies (canonical_name) VALUES (?)",
        (canonical_name,),
    )
    canonical_id = cur.lastrowid
    conn.execute(
        """
        INSERT INTO raw_leads (
            company_name, source, source_url, initial_description,
            category_hint, country, canonical_id, website
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Smoke Test Robotics",
            "smoke_test",
            "https://example.test/smoke",
            "Temporary smoke-test lead for promotion idempotency.",
            "Autonomy and Unmanned Systems",
            "US",
            canonical_id,
            "https://smoke-test-robotics.example",
        ),
    )
    conn.commit()
    _assert_count(conn, "SELECT COUNT(*) FROM companies", 0, "initial companies")
    _assert_count(conn, "SELECT COUNT(*) FROM raw_leads WHERE enriched = 1", 0, "initial promoted raw leads")
    conn.close()

    run(dry_run=True, limit=None, db_path=db_path)
    conn = sqlite3.connect(db_path)
    _assert_count(conn, "SELECT COUNT(*) FROM companies", 0, "dry-run companies")
    _assert_count(conn, "SELECT COUNT(*) FROM canonical_companies WHERE company_id IS NOT NULL", 0, "dry-run canonical links")
    _assert_count(conn, "SELECT COUNT(*) FROM raw_leads WHERE enriched = 1", 0, "dry-run promoted raw leads")
    conn.close()

    run(dry_run=False, limit=None, db_path=db_path)
    conn = sqlite3.connect(db_path)
    _assert_count(conn, "SELECT COUNT(*) FROM companies", 1, "first promotion companies")
    _assert_count(conn, "SELECT COUNT(*) FROM canonical_companies WHERE company_id IS NOT NULL", 1, "first promotion canonical links")
    _assert_count(conn, "SELECT COUNT(*) FROM raw_leads WHERE enriched = 1", 1, "first promotion promoted raw leads")
    conn.close()

    run(dry_run=False, limit=None, db_path=db_path)
    conn = sqlite3.connect(db_path)
    _assert_count(conn, "SELECT COUNT(*) FROM companies", 1, "second promotion companies")
    _assert_count(conn, "SELECT COUNT(*) FROM canonical_companies WHERE company_id IS NOT NULL", 1, "second promotion canonical links")
    _assert_count(conn, "SELECT COUNT(*) FROM raw_leads WHERE enriched = 1", 1, "second promotion promoted raw leads")
    conn.close()

    print(f"\nTemp promotion smoke test passed: {db_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Promote canonical companies from raw_leads into the companies table.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be promoted without writing anything.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Only process the first N canonicals (alphabetical order).",
    )
    parser.add_argument(
        "--db", type=Path, default=DB_PATH,
        help=f"SQLite database path. Defaults to {DB_PATH}.",
    )
    parser.add_argument(
        "--smoke-test-temp", action="store_true",
        help="Run a local /tmp promotion idempotency smoke test without touching the default DB.",
    )
    args = parser.parse_args()
    if args.smoke_test_temp:
        _run_temp_smoke_test()
        return
    run(dry_run=args.dry_run, limit=args.limit, db_path=args.db)


if __name__ == "__main__":
    main()
