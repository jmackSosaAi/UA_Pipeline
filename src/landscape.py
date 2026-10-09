"""
Competitive landscape queries — query-time only, no new stored state.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "companies.db"


def similar_companies(
    company_id: int,
    top_n: int = 5,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """
    Return up to top_n most similar companies to the given company_id.

    Similarity is defined as same primary_category, ranked by total_score
    descending. Excludes the company itself, portfolio companies, and
    excluded entities (portfolio_company=1).

    Accepts an optional open connection for use within a larger pipeline
    (avoids re-opening the DB). If none is passed, opens and closes one.
    """
    _close = conn is None
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT primary_category FROM companies WHERE id = ?", (company_id,)
    ).fetchone()

    if not row or not row["primary_category"]:
        if _close:
            conn.close()
        return []

    category = row["primary_category"]

    peers = conn.execute(
        """
        SELECT
            id, name, total_score, tier, tier_label,
            primary_category, description, website,
            hq_country, traction_signals, dossier_at
        FROM companies
        WHERE primary_category = ?
          AND id != ?
          AND (portfolio_company IS NULL OR portfolio_company = 0)
        ORDER BY total_score DESC
        LIMIT ?
        """,
        (category, company_id, top_n),
    ).fetchall()

    result = []
    for p in peers:
        d = dict(p)
        d["traction_signals"] = json.loads(d.get("traction_signals") or "[]")
        result.append(d)

    if _close:
        conn.close()
    return result
