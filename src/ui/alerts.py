"""Changelog / alerts data layer for the Home page."""
import sqlite3
from pathlib import Path

_ROOT   = Path(__file__).parent.parent.parent
DB_PATH = _ROOT / "data" / "companies.db"

_KEY = "last_viewed_at"


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()


def get_last_viewed() -> str | None:
    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    row = conn.execute(
        "SELECT value FROM dashboard_state WHERE key = ?", (_KEY,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def update_last_viewed() -> None:
    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO dashboard_state (key, value) VALUES (?, datetime('now'))",
        (_KEY,),
    )
    conn.commit()
    conn.close()


def get_changes_since(since: str) -> dict[str, list[dict]]:
    """
    Returns dict of change lists since `since` (ISO datetime string).
    Keys: new_companies, newly_enriched, score_changes, new_tier1,
          status_changes, outreach_updates.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_table(conn)

    result: dict[str, list[dict]] = {
        "new_companies":   [],
        "newly_enriched":  [],
        "score_changes":   [],
        "new_tier1":       [],
        "status_changes":  [],
        "outreach_updates": [],
    }

    try:
        rows = conn.execute(
            "SELECT company_name, source, discovered_at FROM raw_leads "
            "WHERE discovered_at > ? ORDER BY discovered_at DESC",
            (since,),
        ).fetchall()
        result["new_companies"] = [dict(r) for r in rows]
    except Exception:
        pass

    try:
        rows = conn.execute(
            """SELECT name, total_score, tier, primary_category, enriched_at
               FROM companies
               WHERE enriched_at > ?
                 AND (enrich_error IS NULL OR enrich_error = '')
                 AND (portfolio_company IS NULL OR portfolio_company = 0)
               ORDER BY enriched_at DESC""",
            (since,),
        ).fetchall()
        result["newly_enriched"] = [dict(r) for r in rows]
    except Exception:
        pass

    try:
        rows = conn.execute(
            """SELECT c.name, c.total_score, c.tier, c.primary_category, c.scored_at,
                      (SELECT sh.total_score FROM score_history sh
                       WHERE sh.company_id = c.id
                       ORDER BY sh.scored_at DESC LIMIT 1 OFFSET 1) AS prev_score
               FROM companies c
               WHERE c.scored_at > ?
                 AND (c.portfolio_company IS NULL OR c.portfolio_company = 0)
               ORDER BY c.total_score DESC NULLS LAST""",
            (since,),
        ).fetchall()
        result["score_changes"] = [dict(r) for r in rows]
    except Exception:
        pass

    result["new_tier1"] = [r for r in result["score_changes"] if r.get("tier") == 1]

    try:
        rows = conn.execute(
            """SELECT name, status, status_updated_at, total_score, tier
               FROM companies
               WHERE status_updated_at > ?
                 AND (portfolio_company IS NULL OR portfolio_company = 0)
               ORDER BY status_updated_at DESC""",
            (since,),
        ).fetchall()
        result["status_changes"] = [dict(r) for r in rows]
    except Exception:
        pass

    try:
        rows = conn.execute(
            """SELECT name, outreach_status, outreach_date_last_contact,
                      outreach_owner, outreach_next_action
               FROM companies
               WHERE outreach_date_last_contact > ?
                 AND (portfolio_company IS NULL OR portfolio_company = 0)
               ORDER BY outreach_date_last_contact DESC""",
            (since,),
        ).fetchall()
        result["outreach_updates"] = [dict(r) for r in rows]
    except Exception:
        pass

    conn.close()
    return result
