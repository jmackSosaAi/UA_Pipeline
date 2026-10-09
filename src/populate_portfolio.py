"""
Populate the portfolio table with the fund's current portfolio companies.
Run once (or re-run to refresh data) from the project root:
    python src/populate_portfolio.py
"""
import json
import sqlite3
from pathlib import Path

from db.migrate import migrate as migrate_database

DB_PATH = Path(__file__).parent.parent / "data" / "companies.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    slug             TEXT    NOT NULL UNIQUE,
    category_tag     TEXT,
    location         TEXT,
    founded_year     INTEGER,
    description      TEXT,
    full_description TEXT,
    products         TEXT,   -- JSON [{name, description}]
    funding_total    TEXT,
    co_investors     TEXT,   -- JSON [str]
    team             TEXT,   -- JSON [{name, role}]
    traction         TEXT,
    website          TEXT,
    is_stealth       INTEGER DEFAULT 0,
    primary_category TEXT,   -- matches companies.primary_category for pipeline linkage
    updated_at       TEXT    DEFAULT (datetime('now'))
)
"""

# Portfolio companies load from config/portfolio.json (git-ignored; a list of objects with the
# columns above). Without it the table is left empty.
_PORTFOLIO_PATH = Path(__file__).parent.parent / "config" / "portfolio.json"
COMPANIES = json.loads(_PORTFOLIO_PATH.read_text()) if _PORTFOLIO_PATH.exists() else []


def run() -> None:
    migrate_database(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)

    for co in COMPANIES:
        existing = conn.execute(
            "SELECT id FROM portfolio WHERE slug = ?", (co["slug"],)
        ).fetchone()

        products_json    = json.dumps(co.get("products", []))
        co_inv_json      = json.dumps(co.get("co_investors", []))
        team_json        = json.dumps(co.get("team", []))

        if existing:
            conn.execute(
                """
                UPDATE portfolio SET
                    name=?, category_tag=?, location=?, founded_year=?,
                    description=?, full_description=?, products=?,
                    funding_total=?, co_investors=?, team=?, traction=?,
                    website=?, is_stealth=?, primary_category=?,
                    updated_at=datetime('now')
                WHERE slug=?
                """,
                (
                    co["name"], co["category_tag"], co.get("location"),
                    co.get("founded_year"), co["description"], co["full_description"],
                    products_json, co.get("funding_total"), co_inv_json, team_json,
                    co.get("traction"), co.get("website"), co.get("is_stealth", 0),
                    co.get("primary_category"), co["slug"],
                ),
            )
            print(f"  updated: {co['name']}")
        else:
            conn.execute(
                """
                INSERT INTO portfolio
                    (name, slug, category_tag, location, founded_year,
                     description, full_description, products, funding_total,
                     co_investors, team, traction, website, is_stealth, primary_category)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    co["name"], co["slug"], co["category_tag"], co.get("location"),
                    co.get("founded_year"), co["description"], co["full_description"],
                    products_json, co.get("funding_total"), co_inv_json, team_json,
                    co.get("traction"), co.get("website"), co.get("is_stealth", 0),
                    co.get("primary_category"),
                ),
            )
            print(f"  inserted: {co['name']}")

    conn.commit()
    conn.close()
    print(f"\nDone — {len(COMPANIES)} portfolio companies in portfolio.")


if __name__ == "__main__":
    run()
