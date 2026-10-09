import sqlite3
import sys
from pathlib import Path

import json

import requests
from bs4 import BeautifulSoup

DB_PATH = Path(__file__).parent.parent.parent / "data" / "companies.db"

_JINA_HEADERS = {
    "Accept": "application/json",
    "X-Return-Format": "text",
}
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def fetch_page_text(url: str, max_chars: int = 15_000) -> str | None:
    """Fetch a URL and return visible text, capped at max_chars.

    Tries Jina Reader first (bypasses bot-blocks), falls back to raw requests.
    """
    # --- Primary: Jina Reader ---
    try:
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers=_JINA_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        payload = json.loads(resp.text)
        text = payload.get("data", {}).get("text", "").strip()
        if text:
            return text[:max_chars]
    except Exception as e:
        print(f"  [jina failed] {url}: {e}", file=sys.stderr)

    # --- Fallback: raw requests + BeautifulSoup ---
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ", strip=True).split())
        if text:
            return text[:max_chars]
    except Exception as e:
        print(f"  [fetch failed] {url}: {e}", file=sys.stderr)

    return None


def _ensure_raw_leads_ext(conn: sqlite3.Connection) -> None:
    """Add website and source_metadata columns to raw_leads if absent."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(raw_leads)")}
    for col, col_type in [("website", "TEXT"), ("source_metadata", "TEXT")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE raw_leads ADD COLUMN {col} {col_type}")
    conn.commit()


def store_lead(
    company_name: str,
    source: str,
    source_url: str | None = None,
    initial_description: str | None = None,
    category_hint: str | None = None,
    country: str | None = None,
    website: str | None = None,
    source_metadata: dict | None = None,
) -> int | None:
    """Insert a lead into raw_leads and assign a canonical company.
    Returns row_id on insertion, None if duplicate."""
    from .dedup import assign_canonical  # local import avoids circular dependency at module load

    with sqlite3.connect(DB_PATH) as conn:
        _ensure_raw_leads_ext(conn)
        try:
            cursor = conn.execute(
                """
                INSERT INTO raw_leads
                    (company_name, source, source_url, initial_description,
                     category_hint, country, website, source_metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_name, source, source_url, initial_description,
                    category_hint, country, website,
                    json.dumps(source_metadata) if source_metadata else None,
                ),
            )
            row_id = cursor.lastrowid
            conn.commit()
            assign_canonical(row_id, company_name, conn)
            return row_id
        except sqlite3.IntegrityError:
            return None
