"""
Shared data layer: DB helpers, cached loaders, and module-level constants.
"""
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import pandas as pd
import streamlit as st

# ── Paths ─────────────────────────────────────────────────────────────────────

_UI_DIR = Path(__file__).parent        # src/ui/
_SRC    = _UI_DIR.parent               # src/
_ROOT   = _SRC.parent                  # project root

DB_PATH = _ROOT / "data" / "companies.db"

# ── thesis / landscape (imported after sys.path is set by dashboard.py) ───────

import thesis
import landscape as _landscape_mod  # noqa: F401 — re-exported for tabs

_DEFENSE_CATEGORIES: list[dict] = thesis.defense_categories()
_PORTFOLIO_TAGS: dict[str, list[str]] = thesis.portfolio_tags()

# ── Pipeline constants ─────────────────────────────────────────────────────────

OUTREACH_STATUSES = [
    "Not Started", "Researching", "Email Sent", "Awaiting Reply",
    "In Conversation", "Meeting Scheduled", "Meeting Completed",
    "Diligence", "Pass", "Closed",
]

OUTREACH_STATUS_COLORS = {
    "Not Started":       "#dc2626",  # red
    "Researching":       "#ea580c",  # orange
    "Email Sent":        "#d97706",  # amber
    "Awaiting Reply":    "#d97706",  # amber
    "In Conversation":   "#2563eb",  # blue
    "Meeting Scheduled": "#2563eb",  # blue
    "Meeting Completed": "#0891b2",  # teal
    "Diligence":         "#059669",  # green
    "Pass":              "#6b7280",  # gray
    "Closed":            "#6b7280",  # gray
}

STATUS_STAGES = [
    "sourced", "screened", "interesting",
    "meeting_scheduled", "in_diligence", "pass", "invested",
]
STATUS_LABELS = {
    "sourced":           "Sourced",
    "screened":          "Screened",
    "interesting":       "Interesting",
    "meeting_scheduled": "Meeting Scheduled",
    "in_diligence":      "In Diligence",
    "pass":              "Pass",
    "invested":          "Invested",
}
DIM_LABELS = {
    "defense_relevance":  "Defense Relevance",
    "technical_founders": "Technical Founders",
    "post_war_durable":   "Post-War Durable",
    "nato_exportable":    "NATO Exportable",
    "stage_fit":          "Stage Fit",
    "shipped_product":    "Shipped Product",
}

# ── Defense industry market data (HOME + INDUSTRY DATA) ───────────────────────

_IND_DATA = {
    "Military Drones/UAV":    {2018:9.5,  2019:10.2, 2020:10.8, 2021:11.5, 2022:12.5, 2023:14.1, 2024:16.1, 2025:18.2},
    "Electronic Warfare":     {2018:12.5, 2019:14.1, 2020:13.8, 2021:14.5, 2022:15.2, 2023:16.0, 2024:16.8, 2025:17.5},
    "Cybersecurity (Defense)":{2018:14.0, 2019:15.5, 2020:17.2, 2021:19.0, 2022:21.5, 2023:24.0, 2024:27.5, 2025:31.0},
    "Autonomous Systems":     {2018:7.0,  2019:8.2,  2020:9.5,  2021:11.0, 2022:13.5, 2023:16.0, 2024:19.5, 2025:24.0},
    "Counter-Drone/C-UAS":   {2018:0.4,  2019:0.6,  2020:0.8,  2021:1.3,  2022:2.2,  2023:3.5,  2024:5.0,  2025:6.6},
    "Military Robotics":      {2018:2.5,  2019:3.0,  2020:3.8,  2021:4.5,  2022:5.5,  2023:7.0,  2024:9.0,  2025:12.0},
    "C4ISR":                  {2018:28.0, 2019:29.5, 2020:31.0, 2021:33.0, 2022:35.5, 2023:38.0, 2024:41.0, 2025:45.0},
}

_IND_COLORS = {
    "Military Drones/UAV":    "#111111",
    "Electronic Warfare":     "#dc2626",
    "Cybersecurity (Defense)":"#059669",
    "Autonomous Systems":     "#2563eb",
    "Counter-Drone/C-UAS":   "#d97706",
    "Military Robotics":      "#7c3aed",
    "C4ISR":                  "#6b7280",
}

# ── Raw DB access ─────────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _read(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def _write(sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(sql, params)
    conn.commit()
    conn.close()

# ── Cached loaders ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=15)
def search_companies(query: str, limit: int = 10) -> list[dict]:
    """Multi-field global search across companies + founders.

    Searches:
      - companies.name (case-insensitive substring)
      - companies.description (substring)
      - companies.primary_category (substring)
      - companies.primary_sector (substring)
      - companies.hq_country (substring)
      - founders.name (substring; joined via founders.company_id)

    Ranking (lower rank_key sorts first):
      1. exact name match           (rank 0)
      2. name starts-with match     (rank 1)
      3. name substring             (rank 2)
      4. founders.name match        (rank 3)
      5. category / sector match    (rank 4)
      6. description / country      (rank 5)

    Returns a list of dicts: {id, name, score, tier, category, match_field}.
    Empty list for empty/whitespace queries.
    """
    q = (query or "").strip()
    if not q:
        return []

    pat = f"%{q}%"
    starts = f"{q}%"
    rows = _read(
        """
        WITH matches AS (
            SELECT c.id,
                   c.name,
                   c.total_score,
                   c.tier,
                   c.primary_category,
                   CASE
                     WHEN LOWER(c.name) = LOWER(?)              THEN 0
                     WHEN LOWER(c.name) LIKE LOWER(?)           THEN 1
                     WHEN LOWER(c.name) LIKE LOWER(?)           THEN 2
                     WHEN EXISTS (
                       SELECT 1 FROM founders f
                        WHERE f.company_id = c.id
                          AND LOWER(f.name) LIKE LOWER(?)
                     )                                          THEN 3
                     WHEN LOWER(c.primary_category) LIKE LOWER(?)
                          OR LOWER(c.primary_sector) LIKE LOWER(?) THEN 4
                     WHEN LOWER(c.description) LIKE LOWER(?)
                          OR LOWER(c.hq_country) LIKE LOWER(?)  THEN 5
                     ELSE 99
                   END AS rank_key,
                   CASE
                     WHEN LOWER(c.name) LIKE LOWER(?)           THEN 'name'
                     WHEN EXISTS (
                       SELECT 1 FROM founders f
                        WHERE f.company_id = c.id
                          AND LOWER(f.name) LIKE LOWER(?)
                     )                                          THEN 'founders'
                     WHEN LOWER(c.primary_category) LIKE LOWER(?)
                                                                THEN 'category'
                     WHEN LOWER(c.primary_sector) LIKE LOWER(?) THEN 'sector'
                     WHEN LOWER(c.hq_country) LIKE LOWER(?)     THEN 'country'
                     WHEN LOWER(c.description) LIKE LOWER(?)    THEN 'description'
                     ELSE NULL
                   END AS match_field
              FROM companies c
             WHERE c.portfolio_company IS NULL OR c.portfolio_company = 0
        )
        SELECT id, name, total_score AS score, tier,
               primary_category AS category, match_field
          FROM matches
         WHERE rank_key < 99
         ORDER BY rank_key,
                  COALESCE(total_score, 0) DESC,
                  name
         LIMIT ?
        """,
        (
            q, starts, pat,                         # rank_key: exact, starts, substring
            pat,                                    # rank_key: founders LIKE
            pat, pat,                               # rank_key: category, sector
            pat, pat,                               # rank_key: description, country
            pat,                                    # match_field: name LIKE
            pat,                                    # match_field: founders LIKE
            pat,                                    # match_field: category LIKE
            pat,                                    # match_field: sector LIKE
            pat,                                    # match_field: country LIKE
            pat,                                    # match_field: description LIKE
            limit,
        ),
    )
    return rows


def load_companies() -> pd.DataFrame:
    rows = _read("""
        SELECT id, name, website, source, hq_country,
               total_score, tier, tier_label,
               primary_category, traction_signals,
               funding_amount, mention_count, tender_count,
               dossier_at, memo, enriched_at,
               status, user_notes, score_breakdown,
               description, dossier_summary,
               source_urls, cross_validated, portfolio_company,
               scored_at,
               outreach_status, outreach_date_first_contact,
               outreach_date_last_contact, outreach_owner,
               outreach_notes, outreach_next_action, outreach_next_action_date
        FROM companies
        WHERE portfolio_company IS NULL OR portfolio_company = 0
        ORDER BY total_score DESC, name
    """)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


@st.cache_data(ttl=60)
def load_score_trends() -> dict[int, str]:
    rows = _read("""
        SELECT company_id, total_score
        FROM score_history
        ORDER BY company_id, scored_at DESC
    """)
    by_co: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        by_co[r["company_id"]].append(r["total_score"])
    trends = {}
    for cid, scores in by_co.items():
        if len(scores) < 2:
            trends[cid] = "→"
        elif scores[0] > scores[1]:
            trends[cid] = "↑"
        elif scores[0] < scores[1]:
            trends[cid] = "↓"
        else:
            trends[cid] = "→"
    return trends


@st.cache_data(ttl=120)
def load_portfolio_categories() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for cat in _DEFENSE_CATEGORIES:
        kws = [k.lower() for k in cat["keywords"]]
        matched = [
            co for co, tags in _PORTFOLIO_TAGS.items()
            if any(kw in " ".join(tags).lower() for kw in kws)
        ]
        result[cat["name"]] = matched
    return result


@st.cache_data(ttl=15)
def load_company(company_id: int) -> dict | None:
    rows = _read("SELECT * FROM companies WHERE id = ?", (company_id,))
    return rows[0] if rows else None


@st.cache_data(ttl=30)
def load_score_history(company_id: int) -> list[dict]:
    return _read(
        "SELECT total_score, scored_at FROM score_history "
        "WHERE company_id = ? ORDER BY scored_at",
        (company_id,),
    )


@st.cache_data(ttl=60)
def load_portfolio() -> list[dict]:
    rows = _read("SELECT * FROM portfolio ORDER BY id")
    for r in rows:
        for field in ("products", "co_investors", "team"):
            r[field] = json.loads(r.get(field) or "[]")
    return rows


# ── Press tab: articles + mentions ─────────────────────────────────────────────


def _press_filter_clause(filters: dict) -> tuple[str, list]:
    """Translate the press-tab filters dict into a SQL WHERE clause and params."""
    where: list[str] = []
    params: list = []

    pubs = filters.get("publications") or []
    if pubs:
        where.append(f"publication IN ({','.join('?' * len(pubs))})")
        params.extend(pubs)

    sectors = filters.get("sectors") or []
    if sectors:
        # sector_tags is stored as a JSON array string: '["foo","bar"]'.
        # Match each tag as a quoted substring — cheap and avoids JSON1 dependence.
        clauses = []
        for s in sectors:
            clauses.append("sector_tags LIKE ?")
            params.append(f'%"{s}"%')
        where.append("(" + " OR ".join(clauses) + ")")

    min_rel = filters.get("min_relevance")
    if min_rel is not None and min_rel > 0:
        where.append("relevance_score IS NOT NULL AND relevance_score >= ?")
        params.append(float(min_rel))

    date_range = filters.get("date_range") or "all"
    days_map = {"7d": 7, "30d": 30, "90d": 90}
    if date_range in days_map:
        where.append("COALESCE(published_at, processed_at) >= datetime('now', ?)")
        params.append(f"-{days_map[date_range]} days")

    return ("WHERE " + " AND ".join(where) if where else ""), params


def load_articles(filters: dict, limit: int = 20, offset: int = 0) -> list[dict]:
    """Return articles matching `filters`, newest first, with company list attached.

    Each row includes:
      - all processed_articles columns (sector_tags as raw JSON)
      - sector_tags_list — parsed list[str] (or [] if NULL/invalid)
      - companies — list of {company_id, canonical_id, company_name, context}
    """
    where_sql, params = _press_filter_clause(filters)
    sql = f"""
        SELECT id, url, title, publication, published_at, processed_at,
               summary, relevance_score, sector_tags, full_text_preview,
               byline, companies_found,
               COALESCE(published_at, processed_at) AS sort_at
          FROM processed_articles
          {where_sql}
         ORDER BY sort_at DESC, id DESC
         LIMIT ? OFFSET ?
    """
    articles = _read(sql, tuple(params + [int(limit), int(offset)]))
    if not articles:
        return []

    article_ids = [a["id"] for a in articles]
    placeholders = ",".join("?" * len(article_ids))
    company_rows = _read(
        f"""
        SELECT ac.article_id, ac.company_id, ac.canonical_id,
               ac.company_name, ac.context
          FROM article_companies ac
         WHERE ac.article_id IN ({placeholders})
         ORDER BY ac.id
        """,
        tuple(article_ids),
    )
    by_article: dict[int, list[dict]] = {}
    for r in company_rows:
        by_article.setdefault(r["article_id"], []).append({
            "company_id":   r["company_id"],
            "canonical_id": r["canonical_id"],
            "company_name": r["company_name"],
            "context":      r["context"],
        })

    for a in articles:
        a["companies"] = by_article.get(a["id"], [])
        try:
            a["sector_tags_list"] = json.loads(a["sector_tags"]) if a["sector_tags"] else []
        except (TypeError, ValueError):
            a["sector_tags_list"] = []
    return articles


def count_articles(filters: dict) -> int:
    """Count articles matching `filters` — used for pagination."""
    where_sql, params = _press_filter_clause(filters)
    rows = _read(
        f"SELECT COUNT(*) AS n FROM processed_articles {where_sql}",
        tuple(params),
    )
    return int(rows[0]["n"]) if rows else 0


@st.cache_data(ttl=30)
def load_publications() -> list[str]:
    """Distinct publications from processed_articles, alphabetical."""
    rows = _read(
        "SELECT DISTINCT publication FROM processed_articles "
        "WHERE publication IS NOT NULL ORDER BY publication"
    )
    return [r["publication"] for r in rows if r["publication"]]


@st.cache_data(ttl=30)
def load_press_metrics() -> dict:
    """Top-of-tab counts: total articles / last 7d / unique companies / last-30d mentions."""
    rows = _read("""
        SELECT
          (SELECT COUNT(*) FROM processed_articles) AS total_articles,
          (SELECT COUNT(*) FROM processed_articles
              WHERE COALESCE(published_at, processed_at) >= datetime('now', '-7 days')
          ) AS articles_last_7d,
          (SELECT COUNT(DISTINCT canonical_id) FROM article_companies
              WHERE canonical_id IS NOT NULL
          ) AS total_companies_mentioned,
          (SELECT COUNT(*) FROM article_companies
              WHERE mentioned_at >= datetime('now', '-30 days')
          ) AS mentions_last_30d
    """)
    return rows[0] if rows else {
        "total_articles": 0, "articles_last_7d": 0,
        "total_companies_mentioned": 0, "mentions_last_30d": 0,
    }


# ── Press tab: per-company mentions and buzz score ────────────────────────────


def _resolve_canonical_id(conn: sqlite3.Connection, company_id: int) -> int | None:
    """Find a company's canonical_companies.id via name normalisation.

    There is no FK from companies → canonical_companies; the link is
    `normalize_name(companies.name) == canonical_companies.canonical_name`,
    same convention used by collectors.dedup and backfill_company_ids.
    """
    row = conn.execute("SELECT name FROM companies WHERE id = ?", (company_id,)).fetchone()
    if not row or not row["name"]:
        return None
    from collectors.dedup import normalize_name  # local import — avoid load-time cost
    canon_name = normalize_name(row["name"])
    if not canon_name:
        return None
    found = conn.execute(
        "SELECT id FROM canonical_companies WHERE canonical_name = ?",
        (canon_name,),
    ).fetchone()
    return found["id"] if found else None


def load_company_press_mentions(company_id: int, limit: int = 10) -> list[dict]:
    """Return recent articles mentioning this company.

    Match logic — union of two paths so pre-promotion and post-promotion both work:
      1. article_companies.company_id == company_id (direct FK, set by backfill)
      2. article_companies.canonical_id == resolve_canonical(company_id)
         — catches mentions stored before the company was promoted
    """
    conn = get_connection()
    try:
        canonical_id = _resolve_canonical_id(conn, company_id)
        params: list = [company_id]
        canon_clause = ""
        if canonical_id is not None:
            canon_clause = " OR ac.canonical_id = ?"
            params.append(canonical_id)

        rows = conn.execute(
            f"""
            SELECT pa.id          AS article_id,
                   pa.title,
                   pa.url,
                   pa.publication,
                   pa.published_at,
                   pa.processed_at,
                   pa.summary,
                   pa.relevance_score,
                   pa.sector_tags,
                   pa.byline,
                   ac.context
              FROM article_companies ac
              JOIN processed_articles pa ON pa.id = ac.article_id
             WHERE ac.company_id = ?{canon_clause}
             GROUP BY pa.id
             ORDER BY COALESCE(pa.published_at, pa.processed_at) DESC, pa.id DESC
             LIMIT ?
            """,
            tuple(params + [int(limit)]),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["sector_tags_list"] = json.loads(d["sector_tags"]) if d["sector_tags"] else []
        except (TypeError, ValueError):
            d["sector_tags_list"] = []
        out.append(d)
    return out


def compute_buzz_score(company_id: int, days: int = 30) -> dict:
    """Mention count / publication count / avg relevance / trend over `days`.

    Trend compares last 7 days vs the prior 7 days (only meaningful when
    days >= 14; below that we fall back to "steady").
    """
    conn = get_connection()
    try:
        canonical_id = _resolve_canonical_id(conn, company_id)
        canon_clause = "OR ac.canonical_id = ?" if canonical_id is not None else ""
        canon_args: tuple = (canonical_id,) if canonical_id is not None else ()

        win_arg = (f"-{int(days)} days",)

        agg = conn.execute(
            f"""
            SELECT COUNT(*) AS mention_count,
                   COUNT(DISTINCT pa.publication) AS publication_count,
                   AVG(pa.relevance_score) AS avg_relevance
              FROM article_companies ac
              JOIN processed_articles pa ON pa.id = ac.article_id
             WHERE (ac.company_id = ? {canon_clause})
               AND ac.mentioned_at >= datetime('now', ?)
            """,
            (company_id, *canon_args, *win_arg),
        ).fetchone()

        recent_n = conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM article_companies ac
             WHERE (ac.company_id = ? {canon_clause})
               AND ac.mentioned_at >= datetime('now', '-7 days')
            """,
            (company_id, *canon_args),
        ).fetchone()["n"]

        prior_n = conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM article_companies ac
             WHERE (ac.company_id = ? {canon_clause})
               AND ac.mentioned_at >= datetime('now', '-14 days')
               AND ac.mentioned_at  < datetime('now', '-7 days')
            """,
            (company_id, *canon_args),
        ).fetchone()["n"]
    finally:
        conn.close()

    if recent_n > prior_n:
        trend = "rising"
    elif recent_n < prior_n:
        trend = "declining"
    else:
        trend = "steady"

    return {
        "mention_count":     int(agg["mention_count"] or 0),
        "publication_count": int(agg["publication_count"] or 0),
        "avg_relevance":     float(agg["avg_relevance"]) if agg["avg_relevance"] is not None else 0.0,
        "trend":             trend,
    }


@st.cache_data(ttl=30)
def load_recent_mention_counts(days: int = 30) -> dict[int, int]:
    """Bulk: {company_id: mention_count} across all companies, last `days` days.

    Joins article_companies → companies via direct company_id link AND via
    canonical name match (for unpromoted leads that have a canonical_id).
    Used to decorate company cards in the deal-flow grid.
    """
    rows = _read(
        f"""
        WITH window_mentions AS (
            SELECT ac.company_id, ac.canonical_id
              FROM article_companies ac
             WHERE ac.mentioned_at >= datetime('now', '-{int(days)} days')
        ),
        canon_to_company AS (
            -- map canonical_companies.id → companies.id by normalized name match.
            -- pure SQL using LOWER(); the normalize_name suffix-stripping is best-effort
            -- here, but covers the common case of identical names. Anything more nuanced
            -- gets caught by the direct company_id link.
            SELECT cc.id AS canonical_id, c.id AS company_id
              FROM canonical_companies cc
              JOIN companies c ON LOWER(c.name) = cc.canonical_name
        )
        SELECT company_id, COUNT(*) AS n FROM (
            SELECT company_id FROM window_mentions WHERE company_id IS NOT NULL
            UNION ALL
            SELECT ctc.company_id
              FROM window_mentions wm
              JOIN canon_to_company ctc ON ctc.canonical_id = wm.canonical_id
             WHERE wm.company_id IS NULL
        )
        GROUP BY company_id
        """
    )
    return {int(r["company_id"]): int(r["n"]) for r in rows if r["company_id"] is not None}


@st.cache_data(ttl=86_400)
def _load_ukraine_geojson() -> dict | None:
    import requests as _req
    try:
        _r = _req.get(
            "https://raw.githubusercontent.com/codeforamerica/click_that_hood/"
            "master/public/data/ukraine.geojson",
            timeout=8,
        )
        if _r.status_code == 200:
            return _r.json()
    except Exception:
        pass
    return None
