"""Idempotent migrations for data/companies.db."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "companies.db"


def migrate() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        # --- raw_leads (original) ---
        conn.execute("""
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
                UNIQUE (company_name, source)
            )
        """)

        # --- canonical_companies ---
        conn.execute("""
            CREATE TABLE IF NOT EXISTS canonical_companies (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_name TEXT    NOT NULL UNIQUE,
                created_at     DATETIME NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # --- raw_leads.canonical_id (added after initial table creation) ---
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(raw_leads)")
        }
        if "canonical_id" not in existing_cols:
            conn.execute("""
                ALTER TABLE raw_leads
                ADD COLUMN canonical_id INTEGER REFERENCES canonical_companies(id)
            """)

        # --- canonical_companies.company_id (added so promote can record
        # website-collision merges; the canonical row points at the
        # existing companies row instead of trying to insert a duplicate.)
        canon_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(canonical_companies)")
        }
        if "company_id" not in canon_cols:
            conn.execute("""
                ALTER TABLE canonical_companies
                ADD COLUMN company_id INTEGER REFERENCES companies(id)
            """)

        conn.commit()
        print("Migration complete: raw_leads, canonical_companies ready.")

    migrate_outreach()
    migrate_dashboard_state()


def migrate_outreach() -> None:
    """Add outreach tracking columns to companies table (idempotent)."""
    with sqlite3.connect(DB_PATH) as conn:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
        added = []
        for col, col_type in [
            ("outreach_status",             "TEXT"),
            ("outreach_date_first_contact", "DATE"),
            ("outreach_date_last_contact",  "DATE"),
            ("outreach_owner",              "TEXT"),
            ("outreach_notes",              "TEXT"),
            ("outreach_next_action",        "TEXT"),
            ("outreach_next_action_date",   "DATE"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
                added.append(col)
        conn.commit()
    if added:
        print(f"Migration complete: added to companies — {', '.join(added)}")
    else:
        print("Migration complete: outreach columns already present.")


def migrate_dashboard_state() -> None:
    """Create dashboard_state key/value table (idempotent)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()


def migrate_founders() -> None:
    """Create founders + contacts tables and add confidence columns to companies.

    Idempotent. Safe to run on every startup.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
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
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_founders_company_id ON founders(company_id)"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id      INTEGER NOT NULL REFERENCES companies(id),
                type            TEXT    NOT NULL,
                value           TEXT    NOT NULL,
                confidence      REAL,
                source_url      TEXT,
                extracted_at    DATETIME DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contacts_company_id ON contacts(company_id)"
        )

        existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
        added = []
        for col, col_type in [
            ("description_confidence",  "REAL"),
            ("hq_country_confidence",   "REAL"),
            ("founded_year_confidence", "REAL"),
            ("funding_confidence",      "REAL"),
            ("founders_confidence",     "REAL"),
            ("funding_summary",         "TEXT"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
                added.append(col)
        conn.commit()
    print(
        "Migration complete: founders, contacts ready"
        + (f"; companies added: {', '.join(added)}" if added else "")
    )


def migrate_press() -> None:
    """Create / extend processed_articles + article_companies (idempotent)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_articles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                url             TEXT    NOT NULL UNIQUE,
                title           TEXT,
                publication     TEXT    NOT NULL,
                published_at    DATETIME,
                processed_at    DATETIME DEFAULT (datetime('now')),
                companies_found INTEGER DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processed_articles_url ON processed_articles(url)"
        )

        # Phase 4A: enrichment columns on processed_articles
        existing = {r[1] for r in conn.execute("PRAGMA table_info(processed_articles)")}
        added = []
        for col, col_type in [
            ("summary",            "TEXT"),
            ("relevance_score",    "REAL"),
            ("sector_tags",        "TEXT"),   # JSON array
            ("full_text_preview",  "TEXT"),
            ("byline",             "TEXT"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE processed_articles ADD COLUMN {col} {col_type}")
                added.append(col)

        # Phase 4A: linking table — articles ↔ companies
        conn.execute("""
            CREATE TABLE IF NOT EXISTS article_companies (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id    INTEGER NOT NULL REFERENCES processed_articles(id),
                company_id    INTEGER REFERENCES companies(id),
                canonical_id  INTEGER REFERENCES canonical_companies(id),
                company_name  TEXT    NOT NULL,
                context       TEXT,
                mentioned_at  DATETIME DEFAULT (datetime('now')),
                UNIQUE(article_id, company_name)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_article_companies_article ON article_companies(article_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_article_companies_company ON article_companies(company_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_article_companies_canonical ON article_companies(canonical_id)"
        )
        conn.commit()

    print(
        "Migration complete: processed_articles + article_companies ready"
        + (f"; added cols: {', '.join(added)}" if added else "")
    )


def migrate_sbir() -> None:
    """Create sbir_awards table for the SBIR/STTR collector (idempotent)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
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
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbir_company      ON sbir_awards(company_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbir_canonical    ON sbir_awards(canonical_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbir_company_name ON sbir_awards(company_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbir_agency       ON sbir_awards(agency)")
        conn.commit()
    print("Migration complete: sbir_awards ready.")


if __name__ == "__main__":
    migrate()
    migrate_dashboard_state()
    migrate_founders()
    migrate_press()
    migrate_sbir()
