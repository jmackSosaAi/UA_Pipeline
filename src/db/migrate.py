"""Canonical, startup-safe SQLite migration path.

This module is intentionally additive and idempotent:
  - creates required tables with CREATE TABLE IF NOT EXISTS
  - adds missing columns with ALTER TABLE ... ADD COLUMN
  - creates indexes with CREATE INDEX IF NOT EXISTS

It never drops tables, deletes rows, renames columns, or rewrites existing data.
Run it before dashboard or pipeline startup to normalize older local databases.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "companies.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: list[tuple[str, str]],
    *,
    verbose: bool = False,
) -> list[str]:
    existing = _columns(conn, table)
    added: list[str] = []
    for name, col_type in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")
            added.append(f"{table}.{name}")
            if verbose:
                print(f"  + column {table}.{name}")
    return added


def _executescript(conn: sqlite3.Connection, script: str) -> None:
    conn.executescript(script.strip() + "\n")


def migrate(db_path: str | Path = DEFAULT_DB_PATH, *, verbose: bool = False) -> dict:
    """Run all canonical migrations.

    Returns a small summary dict useful for preflight output and tests.
    """
    db_path = Path(db_path)
    summary: dict[str, list[str]] = {
        "tables_verified": [],
        "columns_added": [],
        "indexes_verified": [],
    }

    with _connect(db_path) as conn:
        # Core identity and company tables.
        _executescript(
            conn,
            """
            CREATE TABLE IF NOT EXISTS companies (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL,
                website   TEXT    UNIQUE,
                source    TEXT    DEFAULT 'brave1',
                added_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS raw_leads (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name        TEXT    NOT NULL,
                source              TEXT    NOT NULL,
                source_url          TEXT,
                initial_description TEXT,
                category_hint       TEXT,
                country             TEXT,
                discovered_at       DATETIME NOT NULL DEFAULT (datetime('now')),
                enriched            BOOLEAN  NOT NULL DEFAULT 0,
                enriched_at         DATETIME,
                canonical_id        INTEGER REFERENCES canonical_companies(id),
                website             TEXT,
                source_metadata     TEXT,
                UNIQUE(company_name, source)
            );

            CREATE TABLE IF NOT EXISTS canonical_companies (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_name TEXT    NOT NULL UNIQUE,
                created_at     DATETIME NOT NULL DEFAULT (datetime('now')),
                company_id     INTEGER REFERENCES companies(id)
            );
            """,
        )
        summary["tables_verified"].extend(
            ["companies", "raw_leads", "canonical_companies"]
        )

        # Columns consumed by dashboard, collectors, enrichment, scoring,
        # classification, dossier, memo generation, and source-specific flows.
        summary["columns_added"].extend(
            _add_columns(
                conn,
                "companies",
                [
                    # Core/source metadata.
                    ("source", "TEXT DEFAULT 'brave1'"),
                    ("description", "TEXT"),
                    ("hq_country", "TEXT"),
                    ("name_latin", "TEXT"),
                    ("edrpou", "TEXT"),
                    ("procurement_value", "REAL"),
                    ("procuring_entity", "TEXT"),
                    ("tender_count", "INTEGER"),
                    ("funding_amount", "TEXT"),
                    ("investors", "TEXT"),
                    ("source_urls", "TEXT"),
                    ("mention_count", "INTEGER"),
                    ("cross_validated", "INTEGER"),
                    ("portfolio_company", "INTEGER DEFAULT 0"),
                    ("relevance_filter", "TEXT"),
                    ("linkedin_url", "TEXT"),
                    # Pipeline state and analyst notes.
                    ("status", "TEXT"),
                    ("status_updated_at", "TEXT"),
                    ("status_notes", "TEXT"),
                    ("user_notes", "TEXT"),
                    # Enrichment fields.
                    ("product_types", "TEXT"),
                    ("primary_sector", "TEXT"),
                    ("founded_year", "INTEGER"),
                    ("employee_count_est", "TEXT"),
                    ("technologies", "TEXT"),
                    ("notable_products", "TEXT"),
                    ("enriched_at", "TEXT"),
                    ("enrich_error", "TEXT"),
                    ("description_confidence", "REAL"),
                    ("hq_country_confidence", "REAL"),
                    ("founded_year_confidence", "REAL"),
                    ("funding_confidence", "REAL"),
                    ("founders_confidence", "REAL"),
                    ("funding_summary", "TEXT"),
                    # Scoring/classification.
                    ("total_score", "REAL"),
                    ("base_score", "REAL"),
                    ("sector_modifier", "REAL"),
                    ("matched_sectors", "TEXT"),
                    ("score_breakdown", "TEXT"),
                    ("score_justification", "TEXT"),
                    ("scored_at", "TEXT"),
                    ("primary_category", "TEXT"),
                    ("secondary_categories", "TEXT"),
                    ("tier", "INTEGER"),
                    ("tier_label", "TEXT"),
                    ("traction_signals", "TEXT"),
                    ("classified_at", "TEXT"),
                    # Dossier/memo/report generation.
                    ("dossier", "TEXT"),
                    ("dossier_summary", "TEXT"),
                    ("dossier_at", "TEXT"),
                    ("memo", "TEXT"),
                    ("memo_at", "TEXT"),
                    ("llm_paragraph", "TEXT"),
                    ("paragraph_at", "TEXT"),
                    # Outreach tracker.
                    ("outreach_status", "TEXT"),
                    ("outreach_date_first_contact", "DATE"),
                    ("outreach_date_last_contact", "DATE"),
                    ("outreach_owner", "TEXT"),
                    ("outreach_notes", "TEXT"),
                    ("outreach_next_action", "TEXT"),
                    ("outreach_next_action_date", "DATE"),
                    # Apify firmographic enrichment (Phase 2c).
                    # apify_enrichment_metadata stores the verbatim
                    # harvestapi response for forensic audit. apify_
                    # enriched_at marks when the enrichment ran (used
                    # by the cohort selector to skip already-processed
                    # rows). See D-010 in docs/DECISIONS.md.
                    ("apify_enrichment_metadata", "TEXT"),
                    ("apify_enriched_at", "DATETIME"),
                    # Phase 3 — apify_company_employees attempt marker.
                    # Set to datetime('now') after every row processed by
                    # apify_company_employees, regardless of yield. The
                    # cohort filter `apify_employees_attempted_at IS NULL`
                    # makes the run resumable across monthly cycles
                    # without re-paying for already-attempted rows.
                    # See docs/DECISIONS.md D-016, D-017.
                    ("apify_employees_attempted_at", "DATETIME"),
                ],
                verbose=verbose,
            )
        )

        summary["columns_added"].extend(
            _add_columns(
                conn,
                "raw_leads",
                [
                    ("canonical_id", "INTEGER REFERENCES canonical_companies(id)"),
                    ("website", "TEXT"),
                    ("source_metadata", "TEXT"),
                ],
                verbose=verbose,
            )
        )
        summary["columns_added"].extend(
            _add_columns(
                conn,
                "canonical_companies",
                [("company_id", "INTEGER REFERENCES companies(id)")],
                verbose=verbose,
            )
        )

        # Relationship and workflow tables.
        _executescript(
            conn,
            """
            CREATE TABLE IF NOT EXISTS founders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id      INTEGER NOT NULL REFERENCES companies(id),
                name            TEXT    NOT NULL,
                role            TEXT,
                linkedin_url    TEXT,
                twitter_handle  TEXT,
                bio             TEXT,
                background      TEXT,
                confidence      REAL,
                source_url      TEXT,
                extracted_at    DATETIME DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id      INTEGER NOT NULL REFERENCES companies(id),
                type            TEXT    NOT NULL,
                value           TEXT    NOT NULL,
                confidence      REAL,
                source_url      TEXT,
                extracted_at    DATETIME DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS score_history (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id       INTEGER NOT NULL REFERENCES companies(id),
                total_score      REAL    NOT NULL,
                dimension_scores TEXT    NOT NULL,
                scored_at        TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS company_urls (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id  INTEGER NOT NULL REFERENCES companies(id),
                url         TEXT NOT NULL,
                source      TEXT NOT NULL,
                fetched_at  TEXT,
                UNIQUE(company_id, url)
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS dashboard_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """,
        )
        summary["tables_verified"].extend(
            ["founders", "contacts", "score_history", "company_urls", "meta", "dashboard_state"]
        )

        # Press collector/read-model tables.
        _executescript(
            conn,
            """
            CREATE TABLE IF NOT EXISTS processed_articles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                url             TEXT    NOT NULL UNIQUE,
                title           TEXT,
                publication     TEXT    NOT NULL,
                published_at    DATETIME,
                processed_at    DATETIME DEFAULT (datetime('now')),
                companies_found INTEGER DEFAULT 0,
                summary         TEXT,
                relevance_score REAL,
                sector_tags     TEXT,
                full_text_preview TEXT,
                byline          TEXT
            );

            CREATE TABLE IF NOT EXISTS article_companies (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id    INTEGER NOT NULL REFERENCES processed_articles(id),
                company_id    INTEGER REFERENCES companies(id),
                canonical_id  INTEGER REFERENCES canonical_companies(id),
                company_name  TEXT    NOT NULL,
                context       TEXT,
                mentioned_at  DATETIME DEFAULT (datetime('now')),
                UNIQUE(article_id, company_name)
            );
            """,
        )
        summary["tables_verified"].extend(["processed_articles", "article_companies"])
        summary["columns_added"].extend(
            _add_columns(
                conn,
                "processed_articles",
                [
                    ("summary", "TEXT"),
                    ("relevance_score", "REAL"),
                    ("sector_tags", "TEXT"),
                    ("full_text_preview", "TEXT"),
                    ("byline", "TEXT"),
                ],
                verbose=verbose,
            )
        )

        # SBIR awards table.
        _executescript(
            conn,
            """
            CREATE TABLE IF NOT EXISTS sbir_awards (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                award_number  TEXT    UNIQUE,
                company_id    INTEGER REFERENCES companies(id),
                canonical_id  INTEGER REFERENCES canonical_companies(id),
                company_name  TEXT    NOT NULL,
                agency        TEXT    NOT NULL,
                branch        TEXT,
                program       TEXT,
                phase         TEXT,
                amount        REAL,
                topic_code    TEXT,
                topic_title   TEXT,
                abstract      TEXT,
                awarded_at    DATETIME,
                fiscal_year   INTEGER,
                source_url    TEXT,
                imported_at   DATETIME DEFAULT (datetime('now'))
            );
            """,
        )
        summary["tables_verified"].append("sbir_awards")

        # Portfolio table used by the Portfolio tab.
        _executescript(
            conn,
            """
            CREATE TABLE IF NOT EXISTS portfolio (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT    NOT NULL,
                slug             TEXT    NOT NULL UNIQUE,
                category_tag     TEXT,
                location         TEXT,
                founded_year     INTEGER,
                description      TEXT,
                full_description TEXT,
                products         TEXT,
                funding_total    TEXT,
                co_investors     TEXT,
                team             TEXT,
                traction         TEXT,
                website          TEXT,
                is_stealth       INTEGER DEFAULT 0,
                primary_category TEXT,
                updated_at       TEXT    DEFAULT (datetime('now'))
            );
            """,
        )
        summary["tables_verified"].append("portfolio")

        # Apify/operator audit tables.
        _executescript(
            conn,
            """
            CREATE TABLE IF NOT EXISTS rejected_orgs (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name             TEXT NOT NULL,
                source                   TEXT NOT NULL,
                rejected_at              DATETIME NOT NULL DEFAULT (datetime('now')),
                rejection_reasons        TEXT NOT NULL,
                raw_metadata             TEXT,
                unrejected_at            DATETIME,
                unrejected_to_lead_id    INTEGER
            );

            CREATE TABLE IF NOT EXISTS linkedin_url_corrections (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id          INTEGER NOT NULL REFERENCES companies(id),
                old_linkedin_url    TEXT NOT NULL,
                returned_org_name   TEXT,
                correction_type     TEXT NOT NULL,
                fuzzy_score         REAL,
                corrected_at        DATETIME NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS linkedin_url_discoveries (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id          INTEGER NOT NULL REFERENCES companies(id),
                attempted_slug      TEXT NOT NULL,
                attempted_url       TEXT NOT NULL,
                outcome             TEXT NOT NULL,
                returned_org_name   TEXT,
                fuzzy_score         REAL,
                candidate_source    TEXT,
                attempted_at        DATETIME NOT NULL DEFAULT (datetime('now'))
            );
            """,
        )
        summary["tables_verified"].extend(
            ["rejected_orgs", "linkedin_url_corrections", "linkedin_url_discoveries"]
        )
        # Backfill candidate_source on databases that have the table from
        # before the column was introduced.
        summary["columns_added"].extend(
            _add_columns(
                conn,
                "linkedin_url_discoveries",
                [("candidate_source", "TEXT")],
                verbose=verbose,
            )
        )

        # Indexes. These are deliberately conservative and aligned to existing
        # query patterns rather than trying to tune every dashboard filter.
        indexes = [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_edrpou ON companies(edrpou) WHERE edrpou IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_companies_source ON companies(source)",
            "CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status)",
            "CREATE INDEX IF NOT EXISTS idx_companies_category ON companies(primary_category)",
            "CREATE INDEX IF NOT EXISTS idx_companies_score ON companies(total_score)",
            "CREATE INDEX IF NOT EXISTS idx_companies_enriched_at ON companies(enriched_at)",
            "CREATE INDEX IF NOT EXISTS idx_companies_employees_attempted_at ON companies(apify_employees_attempted_at)",
            "CREATE INDEX IF NOT EXISTS idx_raw_leads_canonical_id ON raw_leads(canonical_id)",
            "CREATE INDEX IF NOT EXISTS idx_raw_leads_source ON raw_leads(source)",
            "CREATE INDEX IF NOT EXISTS idx_canonical_company_id ON canonical_companies(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_founders_company_id ON founders(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_contacts_company_id ON contacts(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_score_history_company_id ON score_history(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_processed_articles_url ON processed_articles(url)",
            "CREATE INDEX IF NOT EXISTS idx_article_companies_article ON article_companies(article_id)",
            "CREATE INDEX IF NOT EXISTS idx_article_companies_company ON article_companies(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_article_companies_canonical ON article_companies(canonical_id)",
            "CREATE INDEX IF NOT EXISTS idx_sbir_company ON sbir_awards(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_sbir_canonical ON sbir_awards(canonical_id)",
            "CREATE INDEX IF NOT EXISTS idx_sbir_company_name ON sbir_awards(company_name)",
            "CREATE INDEX IF NOT EXISTS idx_sbir_agency ON sbir_awards(agency)",
            "CREATE INDEX IF NOT EXISTS idx_rejected_orgs_source ON rejected_orgs(source)",
            "CREATE INDEX IF NOT EXISTS idx_rejected_orgs_company_name ON rejected_orgs(company_name)",
            "CREATE INDEX IF NOT EXISTS idx_lic_company_id ON linkedin_url_corrections(company_id)",
            "CREATE INDEX IF NOT EXISTS idx_lic_correction_type ON linkedin_url_corrections(correction_type)",
            "CREATE INDEX IF NOT EXISTS idx_linkedin_discoveries_outcome ON linkedin_url_discoveries(outcome)",
            "CREATE INDEX IF NOT EXISTS idx_linkedin_discoveries_company_id ON linkedin_url_discoveries(company_id)",
        ]
        for statement in indexes:
            conn.execute(statement)
        summary["indexes_verified"].extend(indexes)

        conn.commit()

    if verbose:
        print(f"migration complete: {db_path}")
        print(f"  tables verified: {len(summary['tables_verified'])}")
        print(f"  columns added:   {len(summary['columns_added'])}")
        print(f"  indexes checked: {len(summary['indexes_verified'])}")

    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run canonical SQLite migrations.")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite DB path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument("--verbose", action="store_true", help="Print migration summary.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    migrate(args.db, verbose=args.verbose)


if __name__ == "__main__":
    main()
