"""
Pulls companies into SQLite from configured sources plus a seed CSV.

Sources (each is idempotent — already-inserted rows are skipped):
  1. Brave1   — https://brave1.gov.ua/en/ecosystem/clusters (paginated Drupal Views)
  2. DIANA    — NATO DIANA 2026 cohort via Wayback Machine archive
  3. ProZorro — Ukraine public procurement API, defense-entity suppliers, last 12 months
  4. seed     — data/seed.csv (name, website columns)

Toggle sources on/off with SOURCES_CONFIG below without touching scraper code.
Deduplication: UNIQUE constraint on website; EDRPOU-first upsert for ProZorro.

TODO (Brave1): If scraper returns 0 companies, the page structure has changed.
      Load CLUSTER_URL in a browser, inspect a company card, and update
      CARD_SELECTORS / NAME_SELECTORS / LINK_SELECTORS in _parse_cards() below.

# TODO: Add Crunchbase as a source when API key is available
# Pattern:
#   - API endpoint: https://api.crunchbase.com/v4/searches/organizations
#   - Free tier: need API key from crunchbase.com/api
#   - Query: location in {Ukraine, Poland, Estonia, Germany, UK, Denmark},
#     industries: Defense/Dual-use/Autonomy, stage: Pre-seed/Seed, raised last 24mo
#   - Returns: structured JSON with company name, website, funding, founders
#   - To implement: create scrape_crunchbase(), call it from orchestrator if
#     SOURCES_CONFIG['crunchbase'] == True
"""

import asyncio
import csv
import json
import re
import sqlite3
import time
from pathlib import Path

import aiohttp as _aiohttp
import anthropic
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

import thesis as _thesis
from collectors.base import store_lead
from collectors.source_config import load_article_seed_urls
from db.migrate import migrate as migrate_database

DB_PATH = Path(__file__).parent.parent / "data" / "companies.db"
SEED_CSV = Path(__file__).parent.parent / "data" / "seed.csv"
CLUSTER_URL = "https://brave1.gov.ua/en/ecosystem/clusters"

# Wayback Machine snapshot of the DIANA 2026 cohort page.
# diana.nato.int is behind Cloudflare and returns 403 on direct requests.
DIANA_WAYBACK_URL = (
    "https://web.archive.org/web/20251220082930/"
    "https://www.diana.nato.int/about-diana/2026-cohort-of-companies.html"
)

# ProZorro public procurement API.
# No auth required. Paginated by opaque offset token; descending=1 = newest first.
# Only tenders with procuringEntity.kind == "defense" and status == "complete" are ingested.
_PROZORRO_API = "https://public-api.prozorro.gov.ua/api/2.5"
_ANON_EDRPOU = "88888888"  # placeholder used for classified/anonymized suppliers

# Meta-table keys for the two-frontier watermark system.
# high_water: newest dateModified we've processed — forward runs stop here.
# low_water:  oldest dateModified we've reached  — displayed as coverage floor.
# backfill_cursor: opaque pagination cursor so backfill resumes exactly mid-feed.
_META_HWM = "prozorro_high_water"
_META_LWM = "prozorro_low_water"
_META_BFC = "prozorro_backfill_cursor"

# ── Source toggles ────────────────────────────────────────────────────────────
# Set a source to True to enable it in the ingest run.
SOURCES_CONFIG: dict[str, bool] = {
    "brave1":          False,   # disabled — returns 500 periodically
    "diana":           False,   # disabled — Wayback returns 503 periodically
    "prozorro":        True,    # active — Ukraine defense procurement suppliers
    "articles": True,           # active — Claude extracts companies from curated articles
    # "crunchbase": False,  # not yet implemented — see TODO at top of file
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Drupal Views page size is typically 10–25; treat <5 results as end-of-pages
_MIN_PAGE_SIZE = 5

# Email domains that identify a person's inbox, not a company website
_GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "ukr.net", "i.ua", "outlook.com", "yahoo.com",
    "meta.ua", "bigmir.net", "hotmail.com", "icloud.com", "protonmail.com",
    "mail.ru", "yandex.ru", "rambler.ru",
}

# Lowercase Ukrainian Cyrillic → Latin (KMU 2010, simplified for name matching)
_UA_LAT: dict[str, str] = {
    "а": "a",  "б": "b",  "в": "v",  "г": "h",  "ґ": "g",
    "д": "d",  "е": "e",  "є": "ye", "ж": "zh", "з": "z",
    "и": "y",  "і": "i",  "ї": "yi", "й": "y",  "к": "k",
    "л": "l",  "м": "m",  "н": "n",  "о": "o",  "п": "p",
    "р": "r",  "с": "s",  "т": "t",  "у": "u",  "ф": "f",
    "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ь": "",   "ю": "yu", "я": "ya",
}


# ── Database helpers ─────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    migrate_database(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT    NOT NULL,
            website   TEXT    UNIQUE,
            source    TEXT    DEFAULT 'brave1',
            added_at  TEXT    DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    _migrate_source_col(conn)
    return conn


def _migrate_source_col(conn: sqlite3.Connection) -> None:
    """Ensure the source column exists (pre-existing DBs may pre-date it)."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if "source" not in existing:
        conn.execute("ALTER TABLE companies ADD COLUMN source TEXT DEFAULT 'brave1'")
        conn.commit()


def _migrate_prozorro_cols(conn: sqlite3.Connection) -> None:
    """Add ProZorro-specific metadata columns if they don't already exist."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, col_type in [
        ("edrpou",            "TEXT"),     # Ukraine company registration number
        ("name_latin",        "TEXT"),     # transliterated name for cross-source matching
        ("procurement_value", "REAL"),     # cumulative contract value in UAH
        ("procuring_entity",  "TEXT"),     # most recent defense buyer
        ("tender_count",      "INTEGER"),  # number of defense tender wins (repeat = PMF signal)
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
    conn.commit()
    # Partial unique index — enforces one row per EDRPOU, ignoring NULL
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_edrpou "
        "ON companies(edrpou) WHERE edrpou IS NOT NULL"
    )
    conn.commit()


# ── Resume-state helpers ──────────────────────────────────────────────────────

def _ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.commit()


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def _fetch(url: str) -> requests.Response | None:
    """GET an HTML page. Used by Brave1 and DIANA scrapers."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        return r
    except requests.HTTPError as e:
        print(f"  HTTP {e.response.status_code} fetching {url}")
        return None
    except requests.RequestException as e:
        print(f"  Network error fetching {url}: {e}")
        return None


def _fetch_json(url: str, params: dict | None = None) -> dict | None:
    """GET a JSON API endpoint. Used by the ProZorro scraper."""
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        print(f"  HTTP {e.response.status_code} fetching {url}")
        return None
    except (requests.RequestException, ValueError) as e:
        print(f"  Request error fetching {url}: {e}")
        return None


# ── ProZorro helpers ──────────────────────────────────────────────────────────

def _translit_ua(name: str) -> str:
    """Lowercase Ukrainian Cyrillic → Latin for name matching."""
    return "".join(_UA_LAT.get(c.lower(), c.lower()) for c in name)


def _website_from_contact(contact: dict) -> str | None:
    """
    Extract a company website from a tender contactPoint dict.
    Tries explicit URL first, then infers from email domain.
    Returns None for personal/generic email providers.
    """
    url = (contact.get("url") or "").strip()
    if url.startswith("http"):
        return url.rstrip("/")

    email = (contact.get("email") or "").strip().lower()
    if "@" in email:
        domain = email.split("@")[-1].strip()
        if domain and domain not in _GENERIC_EMAIL_DOMAINS:
            return f"https://{domain}"

    return None


def _upsert_supplier(
    conn: sqlite3.Connection,
    name: str,
    website: str | None,
    edrpou: str | None,
    value_uah: float,
    procuring_entity: str,
    name_latin: str,
) -> tuple[int | None, str]:
    """
    Insert a new ProZorro supplier or update an existing row.
    Lookup order: EDRPOU → website → new insert.
    Returns (row_id, 'inserted' | 'updated' | 'skipped').
    """
    # 1. EDRPOU lookup — most reliable unique key
    if edrpou:
        row = conn.execute(
            "SELECT id FROM companies WHERE edrpou = ?", (edrpou,)
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE companies SET
                       tender_count      = COALESCE(tender_count, 0) + 1,
                       procurement_value = COALESCE(procurement_value, 0.0) + ?,
                       procuring_entity  = ?
                   WHERE id = ?""",
                (value_uah, procuring_entity, row[0]),
            )
            conn.commit()
            return row[0], "updated"

    # 2. Website lookup — catches cross-source duplicates (DIANA, seed)
    if website:
        row = conn.execute(
            "SELECT id FROM companies WHERE website = ?", (website,)
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE companies SET
                       tender_count      = COALESCE(tender_count, 0) + 1,
                       procurement_value = COALESCE(procurement_value, 0.0) + ?,
                       procuring_entity  = ?,
                       edrpou            = COALESCE(edrpou, ?)
                   WHERE id = ?""",
                (value_uah, procuring_entity, edrpou, row[0]),
            )
            conn.commit()
            return row[0], "updated"

    # 3. New row — route through store_lead/dedup pipeline
    row_id = store_lead(
        company_name=name,
        source="prozorro",
        country="Ukraine",
        website=website,
        source_metadata={
            "edrpou":            edrpou,
            "name_latin":        name_latin,
            "procurement_value": value_uah,
            "procuring_entity":  procuring_entity,
            "tender_count":      1,
        },
    )
    if row_id is not None:
        return row_id, "inserted"
    return None, "skipped"


# ── Source scrapers ───────────────────────────────────────────────────────────

def _parse_cards(html: str) -> list[dict]:
    """
    Extract (name, website) pairs from the Brave1 cluster page HTML.

    The page uses Drupal Views — cards are in .views-row elements.
    TODO: If this returns [], open CLUSTER_URL in a browser, right-click a company
          card, and update CARD_SELECTORS / NAME_SELECTORS / LINK_SELECTORS to match.
    """
    soup = BeautifulSoup(html, "lxml")

    CARD_SELECTORS = [".views-row", ".cluster-item", ".company-card", "article.node"]
    NAME_SELECTORS = [
        ".field--name-title", ".views-field-title", "h2", "h3", ".card-title",
    ]
    LINK_SELECTORS = [
        ".field--name-field-website a",
        ".views-field-field-website a",
        ".field--name-field-link a",
        "a[href^='http']:not([href*='brave1.gov.ua'])",
    ]

    cards = []
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if cards:
            break

    results = []
    for card in cards:
        name = ""
        for sel in NAME_SELECTORS:
            el = card.select_one(sel)
            if el:
                name = el.get_text(strip=True)
                break

        website = None
        for sel in LINK_SELECTORS:
            el = card.select_one(sel)
            if el and el.get("href"):
                website = el["href"].strip().rstrip("/")
                break

        if name:
            results.append({"name": name, "website": website})

    return results


def _insert(conn: sqlite3.Connection, name: str, website: str | None, source: str) -> int:
    """Route a company through store_lead/dedup pipeline. Returns 1 if inserted, 0 if duplicate."""
    return 1 if store_lead(company_name=name, source=source, website=website) is not None else 0


def scrape_brave1(conn: sqlite3.Connection) -> int:
    """Paginate through the Brave1 cluster directory. Returns count of new rows inserted."""
    inserted = 0
    for page in range(0, 50):
        url = CLUSTER_URL if page == 0 else f"{CLUSTER_URL}?page={page}"
        print(f"  page {page}: {url}")

        resp = _fetch(url)
        if resp is None:
            if page == 0:
                print("  Brave1 cluster page is unreachable — load from seed.csv instead")
            break

        companies = _parse_cards(resp.text)
        if not companies:
            if page == 0:
                print("  0 cards parsed — page may have changed (see TODO in _parse_cards)")
            break

        for c in companies:
            inserted += _insert(conn, c["name"], c["website"], "brave1")
        conn.commit()

        if len(companies) < _MIN_PAGE_SIZE:
            break
        time.sleep(1)

    return inserted


def scrape_diana(conn: sqlite3.Connection) -> int:
    """
    Fetch the NATO DIANA 2026 cohort from a Wayback Machine snapshot and insert
    new companies. Returns count of rows inserted.

    Card structure (150 companies, all with websites):
      <div class="... cohort-card">
        <h4><a href="WB_PREFIX + original_url">Company Name</a></h4>
        <p>One-sentence description</p>
        <p>Country</p>
      </div>
    """
    print(f"  fetching: {DIANA_WAYBACK_URL}")
    resp = _fetch(DIANA_WAYBACK_URL)
    if resp is None:
        print("  DIANA fetch failed — skipping source")
        return 0

    soup = BeautifulSoup(resp.text, "lxml")
    cards = soup.select(".cohort-card")
    if not cards:
        print("  0 .cohort-card elements found — page structure may have changed")
        return 0

    print(f"  {len(cards)} cards found")
    inserted = 0
    for card in cards:
        a = card.select_one("h4 a")
        if not a:
            continue

        name = a.get_text(strip=True)
        raw_href = a.get("href", "")
        website = re.sub(r"^https?://web\.archive\.org/web/\d+/", "", raw_href)
        website = website.rstrip("/") or None

        if name:
            inserted += _insert(conn, name, website, "diana")

    conn.commit()
    return inserted


# ── Brave1 article harvester ──────────────────────────────────────────────────

_ARTICLE_MAX_CHARS = 25_000  # enough for a long article; haiku has 200K context

_ARTICLE_EXTRACT_TOOL = {
    "name": "extract_defense_companies",
    "description": "Extract defense-tech companies mentioned in the article.",
    "input_schema": {
        "type": "object",
        "properties": {
            "companies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company_name": {
                            "type": "string",
                            "description": "Company name (English preferred).",
                        },
                        "description": {
                            "type": "string",
                            "description": "One sentence: what this company makes or does.",
                        },
                        "website": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "description": "Website URL if mentioned in the article.",
                        },
                        "funding_amount": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "description": "Funding amount if mentioned (e.g. '$3M seed').",
                        },
                        "sector": {
                            "type": "string",
                            "description": (
                                "Primary sector: UAV, EW, counter-drone, autonomy, UGV, "
                                "USV, comms, AI, demining, loitering munition, ISR, C2, other."
                            ),
                        },
                        "investors": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Named investors specifically mentioned for this company.",
                        },
                    },
                    "required": ["company_name", "description", "sector"],
                },
            }
        },
        "required": ["companies"],
    },
}

_ARTICLE_SYSTEM = (
    "You are a defense-sector analyst. Extract every company from the article that is a "
    "defense technology startup, defense manufacturer, or military equipment maker. "
    "Focus on Ukrainian defense-tech, Brave1 ecosystem companies, and startups making "
    "UAVs, drones, EW systems, counter-drone tech, UGVs, USVs, loitering munitions, "
    "autonomous systems, or military C2/comms. "
    "EXCLUDE: investor firms, VCs, government bodies (Ministry of Defence, DTRA, etc.), "
    "media outlets, accelerator programs as organizations, and established foreign primes "
    "(Rheinmetall, BAE Systems, Baykar, Lockheed, Saab, etc). "
    "Only include startups, scale-ups, and manufacturers of military products."
)


def _migrate_brave1_article_cols(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, col_type in [
        ("funding_amount",  "TEXT"),
        ("investors",       "TEXT"),     # JSON array of investor names
        ("source_urls",     "TEXT"),     # JSON array of article URLs
        ("mention_count",   "INTEGER"),
        ("cross_validated", "INTEGER"),  # 1 if found in ≥2 sources
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
    conn.commit()


def _normalize_name(name: str) -> str:
    """Lowercase, strip legal/generic suffixes, collapse whitespace — for deduplication."""
    n = name.lower().strip()
    n = re.sub(
        r"\b(ltd|llc|inc|corp|co|gmbh|llp|plc|ag|bv|sas|srl|sa|oy|ab|as|nv"
        r"|technologies|technology|tech|systems|solutions|group|labs|lab"
        r"|aerospace|defense|defence)\b\.?",
        "", n,
    )
    n = re.sub(r"[^\w\s]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _migrate_portfolio_col(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if "portfolio_company" not in existing:
        conn.execute("ALTER TABLE companies ADD COLUMN portfolio_company INTEGER DEFAULT 0")
        conn.commit()


def _migrate_pipeline_cols(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, col_type in [
        ("status",            "TEXT"),
        ("status_updated_at", "TEXT"),
        ("status_notes",      "TEXT"),
        ("user_notes",        "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
    conn.execute("UPDATE companies SET status='sourced' WHERE status IS NULL")
    conn.commit()


def mark_portfolio_companies(conn: sqlite3.Connection) -> int:
    """
    Cross-check every company in the DB against three portfolio sources:
      1. ``portfolio`` table (canonical metadata — see populate_portfolio.py)
      2. ``thesis_sensitive.yaml → portfolio.companies`` (legacy name list)
      3. ``thesis_sensitive.yaml → stage_exclusions.companies`` (treated as
         portfolio for exclusion purposes — same downstream filter)

    Match logic (D-021, this commit):
      - Normalised name match via `entity_resolution.normalize_company_name`
      - OR normalised website match via `entity_resolution.normalize_website`
        (catches cases where the canonical portfolio entry uses a different
         spelling than the row in `companies` — e.g. "Example Robotics" in
         portfolio vs "Example" in the legacy YAML list).

    On match: sets ``portfolio_company=1`` and bumps ``status`` from
    'sourced'/NULL to 'portfolio' (other status values are preserved
    so manual operator triage isn't clobbered). Idempotent — re-running
    is a no-op on already-marked rows. Returns the count of NEWLY
    marked companies.

    Bulletproof-by-construction: every Apify ingester (discovery,
    enrichment) and the IQT ingest call this at end-of-run, so new
    rows discovered by any sourcing channel get reconciled
    immediately instead of waiting for the next `python -m src.main`
    invocation.
    """
    from entity_resolution import normalize_company_name, normalize_website

    _migrate_portfolio_col(conn)

    # Build normalised-name and normalised-website lookups across all
    # three portfolio sources. Reason strings tell the operator which
    # source matched so audit logs are self-explanatory.
    name_lookup:    dict[str, str] = {}
    website_lookup: dict[str, str] = {}

    # Source 1: portfolio table (canonical).
    try:
        portfolio_rows = conn.execute(
            "SELECT name, website FROM portfolio"
        ).fetchall()
    except sqlite3.OperationalError:
        portfolio_rows = []
    for r in portfolio_rows:
        # Tuple-indexed access to stay agnostic about whether the
        # caller set conn.row_factory.
        p_name = r[0] if not isinstance(r, sqlite3.Row) else r["name"]
        p_website = r[1] if not isinstance(r, sqlite3.Row) else r["website"]
        n_norm = normalize_company_name(p_name)
        w_norm = normalize_website(p_website)
        if n_norm:
            name_lookup.setdefault(n_norm, f"portfolio table: {p_name}")
        if w_norm:
            website_lookup.setdefault(w_norm, f"portfolio website: {p_name}")

    # Source 2: thesis_sensitive.yaml portfolio.companies.
    for c in _thesis.load().get("portfolio", {}).get("companies", []) or []:
        n_norm = normalize_company_name(c.get("name", ""))
        if n_norm:
            name_lookup.setdefault(
                n_norm, f"thesis.yaml portfolio: {c['name']}"
            )

    # Source 3: thesis stage_exclusions (late-stage cos to exclude).
    for c in _thesis.stage_exclusions():
        n_norm = normalize_company_name(c.get("name", ""))
        if n_norm:
            name_lookup.setdefault(
                n_norm,
                f"thesis stage_exclusion: {c['name']} ({c.get('reason', '')})",
            )

    rows = conn.execute(
        "SELECT id, name, website, portfolio_company FROM companies"
    ).fetchall()

    newly_marked = 0
    for row in rows:
        cid = row[0] if not isinstance(row, sqlite3.Row) else row["id"]
        name = row[1] if not isinstance(row, sqlite3.Row) else row["name"]
        website = row[2] if not isinstance(row, sqlite3.Row) else row["website"]
        already = row[3] if not isinstance(row, sqlite3.Row) else row["portfolio_company"]

        reason: str | None = None
        n_norm = normalize_company_name(name)
        w_norm = normalize_website(website)
        if n_norm and n_norm in name_lookup:
            reason = name_lookup[n_norm]
        elif w_norm and w_norm in website_lookup:
            reason = website_lookup[w_norm]
        if reason is None:
            continue
        if already == 1:
            continue  # already marked — silent no-op
        conn.execute(
            "UPDATE companies "
            "SET portfolio_company = 1, "
            "    status = CASE "
            "      WHEN status IS NULL OR status = 'sourced' THEN 'portfolio' "
            "      ELSE status "
            "    END "
            "WHERE id = ?",
            (cid,),
        )
        print(f"  portfolio match: id={cid} name={name!r} → {reason}")
        newly_marked += 1

    conn.commit()
    return newly_marked


def _fetch_article_text(url: str) -> str | None:
    """Fetch a public article URL and return cleaned visible text."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=25)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"    fetch error: {exc}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                      "noscript", "iframe", "form"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return text[:_ARTICLE_MAX_CHARS]


def _extract_companies_from_article(
    client: anthropic.Anthropic, url: str, text: str
) -> list[dict]:
    """Call Claude haiku with forced tool_use to extract companies from article text."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            system=[{"type": "text", "text": _ARTICLE_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            tools=[_ARTICLE_EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "extract_defense_companies"},
            messages=[{"role": "user", "content": f"Article URL: {url}\n\n{text}"}],
        )
    except anthropic.APIError as exc:
        print(f"    Claude API error: {exc}")
        return []

    for block in resp.content:
        if block.type == "tool_use":
            return block.input.get("companies", [])
    return []


def _merge_article_results(url_companies: dict[str, list[dict]]) -> dict[str, dict]:
    """
    Merge extraction results across all articles.
    Key = normalized name. Merges source_urls, investors; prefers non-null website/funding.
    """
    merged: dict[str, dict] = {}

    for url, companies in url_companies.items():
        for c in companies:
            norm = _normalize_name(c.get("company_name", ""))
            if not norm:
                continue
            if norm not in merged:
                merged[norm] = {
                    "name":           c.get("company_name", "").strip(),
                    "description":    c.get("description", "").strip(),
                    "website":        (c.get("website") or "").strip().rstrip("/") or None,
                    "funding_amount": c.get("funding_amount"),
                    "sector":         c.get("sector", ""),
                    "investors":      list(c.get("investors") or []),
                    "source_urls":    [url],
                }
            else:
                e = merged[norm]
                if url not in e["source_urls"]:
                    e["source_urls"].append(url)
                if not e["website"] and c.get("website"):
                    e["website"] = c["website"].strip().rstrip("/") or None
                if not e["funding_amount"] and c.get("funding_amount"):
                    e["funding_amount"] = c["funding_amount"]
                new_inv = set(c.get("investors") or [])
                e["investors"] = list(set(e["investors"]) | new_inv)

    return merged


_PLACEHOLDER_NAMES = frozenset({
    "unknown", "n/a", "na", "tbd", "none", "unnamed", "company",
    "startup", "organization", "firm", "entity",
})


def _is_valid_company_name(name: str) -> bool:
    if not name or len(name.strip()) < 3:
        return False
    stripped = name.strip()
    if stripped.startswith("<") and stripped.endswith(">"):
        return False
    if stripped.lower() in _PLACEHOLDER_NAMES:
        return False
    return True


def _upsert_brave1_article_company(conn: sqlite3.Connection, company: dict) -> str:
    """
    Insert a new brave1_articles company or merge into an existing row.
    Returns 'inserted', 'updated', or 'skipped'.
    """
    name    = company["name"]
    if not _is_valid_company_name(name):
        return "skipped"
    website = company.get("website")
    source_urls   = json.dumps(company["source_urls"])
    mention_count = len(company["source_urls"])
    funding       = company.get("funding_amount")
    investors     = json.dumps(company.get("investors") or [])
    description   = company.get("description")

    # Website match — absorb into existing row (cross-source dedup)
    if website:
        row = conn.execute(
            "SELECT id, source FROM companies WHERE website = ?", (website,)
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE companies SET
                       source_urls   = ?,
                       mention_count = COALESCE(mention_count, 0) + ?,
                       funding_amount= COALESCE(funding_amount, ?),
                       investors     = COALESCE(investors, ?),
                       cross_validated = 1
                   WHERE id = ?""",
                (source_urls, mention_count, funding, investors, row[0]),
            )
            conn.commit()
            return "updated"

    # Name-only match (case-insensitive) among brave1_articles rows
    row = conn.execute(
        "SELECT id FROM companies WHERE source='brave1_articles' AND LOWER(name) = LOWER(?)",
        (name,),
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE companies SET
                   source_urls   = ?,
                   mention_count = COALESCE(mention_count, 0) + ?,
                   funding_amount= COALESCE(funding_amount, ?),
                   investors     = COALESCE(investors, ?)
               WHERE id = ?""",
            (source_urls, mention_count, funding, investors, row[0]),
        )
        conn.commit()
        return "updated"

    # New row — route through store_lead/dedup pipeline
    first_url = company["source_urls"][0] if company["source_urls"] else None
    row_id = store_lead(
        company_name=name,
        source="brave1_articles",
        source_url=first_url,
        initial_description=description,
        category_hint=company.get("sector"),
        website=website,
        source_metadata={
            "funding_amount": company.get("funding_amount"),
            "investors":      company.get("investors") or [],
            "source_urls":    company["source_urls"],
            "mention_count":  mention_count,
        },
    )
    return "inserted" if row_id is not None else "skipped"


def _cross_validate_companies(conn: sqlite3.Connection) -> int:
    """
    Flag brave1_articles companies as cross_validated=1 if a matching company
    exists in any other source (prozorro, seed, diana, brave1).
    Returns count of matches found.
    """
    brave1_rows = conn.execute(
        "SELECT id, name, website FROM companies WHERE source='brave1_articles'"
    ).fetchall()
    other_rows = conn.execute(
        "SELECT id, name, website FROM companies WHERE source != 'brave1_articles'"
    ).fetchall()

    other_norms  = {_normalize_name(r[1]): r[0] for r in other_rows}
    other_sites  = {(r[2] or "").rstrip("/"): r[0]
                    for r in other_rows if r[2]}

    matched = 0
    for b_id, b_name, b_site in brave1_rows:
        hit = False
        if b_site and b_site.rstrip("/") in other_sites:
            hit = True
        if not hit:
            b_norm = _normalize_name(b_name)
            for o_norm in other_norms:
                if b_norm and o_norm and (b_norm == o_norm
                        or (len(b_norm) > 4 and b_norm in o_norm)
                        or (len(o_norm) > 4 and o_norm in b_norm)):
                    hit = True
                    break
        if hit:
            conn.execute(
                "UPDATE companies SET cross_validated = 1 WHERE id = ?", (b_id,)
            )
            matched += 1

    conn.commit()
    return matched


def scrape_articles(conn: sqlite3.Connection) -> int:
    """
    Fetch curated articles (Brave1, DIANA, EUDIS, AIN, etc.), extract defense company
    names via Claude haiku forced tool_use, deduplicate, and store with
    source='brave1_articles'. Returns count of new rows inserted.

    Article URL list lives in config/sources/article_seed_urls.yaml.
    Add new URLs there without touching code.
    """
    _migrate_brave1_article_cols(conn)

    seeds = load_article_seed_urls()
    urls = [s["url"] for s in seeds if s.get("url")]
    if not urls:
        print("  No article sources in config/sources/article_seed_urls.yaml — skipping")
        return 0

    client = anthropic.Anthropic()
    url_companies: dict[str, list[dict]] = {}

    for i, url in enumerate(urls, 1):
        print(f"  [{i}/{len(urls)}] {url}")
        text = _fetch_article_text(url)
        if not text:
            print("    skipped (fetch failed)")
            continue

        companies = _extract_companies_from_article(client, url, text)
        url_companies[url] = companies
        print(f"    {len(text):,} chars → {len(companies)} companies extracted")
        time.sleep(0.3)

    merged = _merge_article_results(url_companies)
    print(f"\n  {len(merged)} unique companies after dedup across all articles")

    inserted = updated = skipped = 0
    for company in merged.values():
        action = _upsert_brave1_article_company(conn, company)
        if action == "inserted":
            inserted += 1
        elif action == "updated":
            updated += 1
        else:
            skipped += 1

    matched = _cross_validate_companies(conn)
    print(f"  Cross-validated: {matched} companies appear in another source")

    return inserted


async def _fetch_detail_async(
    session: "_aiohttp.ClientSession",
    sem: asyncio.Semaphore,
    tender_id: str,
) -> dict | None:
    url = f"{_PROZORRO_API}/tenders/{tender_id}"
    async with sem:
        try:
            async with session.get(url, timeout=_aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except Exception as exc:
            print(f"  Request error fetching {url}: {exc}")
            return None


async def _scrape_prozorro_async(
    conn: sqlite3.Connection,
    mode: str,           # "forward" | "backfill"
    time_limit_sec: int, # 0 = no limit
    target_date: str,    # "YYYY-MM-DD" stop anchor for backfill ("" = no target)
    preview: int,
) -> dict:
    """
    Async ProZorro scraper with forward / backfill modes.

    List pages are fetched sequentially (opaque cursor — cannot parallelise).
    Detail pages within each batch are fetched concurrently: asyncio.gather over
    up to 30 simultaneous requests, gated by a semaphore.

    Forward mode  — starts from the newest item, stops when it reaches the stored
                    high_water_mark (already-processed territory). Fast daily update.
    Backfill mode — resumes from the stored backfill cursor, goes further back in
                    time, stops at time_limit_sec or target_date. Progress is saved
                    after every list page so interrupted runs resume exactly.

    Filters (detail level):
      - procuringEntity.kind == "defense"   (list level pre-filter)
      - status == "complete"                (list level pre-filter)
      - CPV prefix matches whitelist        (detail level — from thesis.yaml)
      - EDRPOU exactly 8 digits             (detail level — drops ФОП sole traders)
    """
    sem = asyncio.Semaphore(30)
    cpv_whitelist = _thesis.cpv_prefixes()
    start_time = time.monotonic()
    deadline = (start_time + time_limit_sec) if time_limit_sec > 0 else None

    high_water = _get_meta(conn, _META_HWM) or ""  # e.g. "2026-04-30T10:30:00"
    low_water  = _get_meta(conn, _META_LWM) or ""  # e.g. "2025-10-15"

    offset = None if mode == "forward" else (_get_meta(conn, _META_BFC) or None)

    inserted = updated = skipped = 0
    total_list_items = 0
    total_processed = 0
    total_cpv_passed = 0
    total_found = 0
    session_newest = ""  # track newest dateModified seen this run (for HWM update)
    session_oldest = ""  # track oldest dateModified seen this run
    preview_rows: list[dict] = []
    cpv_misses: list[dict] = []
    timed_out = False

    async with _aiohttp.ClientSession(
        headers={"User-Agent": _HEADERS["User-Agent"], "Accept": "application/json"},
        connector=_aiohttp.TCPConnector(limit=20),
    ) as session:

        while True:
            # ── Fetch one list page ────────────────────────────────────────────
            params: dict = {
                "descending": "1",
                "limit": "1000",
                "opt_fields": "procuringEntity,status,dateModified,dateCreated",
            }
            if offset:
                params["offset"] = offset

            try:
                async with session.get(
                    f"{_PROZORRO_API}/tenders",
                    params=params,
                    timeout=_aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        print(f"  ProZorro list returned HTTP {resp.status} — stopping")
                        break
                    list_data = await resp.json(content_type=None)
            except Exception as exc:
                print(f"  ProZorro list endpoint error: {exc} — stopping")
                break

            items = list_data.get("data", [])
            if not items:
                print("  Feed exhausted — no more items")
                if mode == "backfill":
                    _set_meta(conn, _META_BFC, "")
                break

            total_list_items += len(items)
            page_newest_mod = items[0].get("dateModified", "")[:19]
            page_oldest_mod = items[-1].get("dateModified", "")[:10]

            if not session_newest or page_newest_mod > session_newest:
                session_newest = page_newest_mod
            if not session_oldest or page_oldest_mod < session_oldest:
                session_oldest = page_oldest_mod

            # ── Pre-filter: which items are worth fetching detail for? ─────────
            # Forward: only items strictly newer than the previous high_water.
            # Backfill / first run: all defense+complete items on the page.
            if mode == "forward" and high_water:
                qualifying_ids: list[str] = [
                    item["id"] for item in items
                    if item.get("dateModified", "")[:19] > high_water
                    and item.get("procuringEntity", {}).get("kind") == "defense"
                    and item.get("status") == "complete"
                ]
            else:
                qualifying_ids = [
                    item["id"] for item in items
                    if item.get("procuringEntity", {}).get("kind") == "defense"
                    and item.get("status") == "complete"
                ]

            # ── Batch-fetch details 30 at a time concurrently ─────────────────
            for batch_start in range(0, len(qualifying_ids), 30):
                batch = qualifying_ids[batch_start : batch_start + 30]
                results = await asyncio.gather(
                    *[_fetch_detail_async(session, sem, tid) for tid in batch]
                )

                for detail_resp in results:
                    if detail_resp is None:
                        continue
                    tender = detail_resp.get("data", {})
                    total_processed += 1

                    if total_processed % 50 == 0:
                        elapsed = int(time.monotonic() - start_time)
                        total_in_db = conn.execute(
                            "SELECT COUNT(*) FROM companies WHERE source='prozorro'"
                        ).fetchone()[0]
                        print(
                            f"  [ProZorro {mode}] Reached {page_oldest_mod} | "
                            f"{total_found} suppliers this session | "
                            f"{total_in_db} total in DB | "
                            f"Elapsed: {elapsed}s"
                        )

                    # CPV filter — whitelist from config/thesis.yaml
                    cpv_ids = [
                        ((it.get("classification") or {}).get("id") or "")
                        for it in tender.get("items", [])
                    ]
                    if not any(
                        cpv.startswith(prefix)
                        for cpv in cpv_ids
                        for prefix in cpv_whitelist
                    ):
                        if len(cpv_misses) < 30:
                            cpv_misses.append({
                                "title": (tender.get("title") or "")[:100],
                                "cpv":   cpv_ids[:4],
                                "buyer": tender.get("procuringEntity", {}).get("name", "")[:60],
                            })
                        continue
                    total_cpv_passed += 1

                    pe_name = tender.get("procuringEntity", {}).get("name", "")
                    tender_title = (tender.get("title") or "").strip()

                    for award in tender.get("awards", []):
                        if award.get("status") != "active":
                            continue
                        value_uah = float((award.get("value") or {}).get("amount") or 0)

                        for supplier in award.get("suppliers", []):
                            edrpou = (
                                (supplier.get("identifier") or {}).get("id") or ""
                            ).strip()
                            if edrpou == _ANON_EDRPOU:
                                continue
                            if not (edrpou.isdigit() and len(edrpou) == 8):
                                continue

                            name = (supplier.get("name") or "").strip()
                            if not name:
                                continue

                            name_latin = _translit_ua(name)
                            total_found += 1

                            if preview > 0:
                                preview_rows.append({
                                    "name":             name,
                                    "name_latin":       name_latin,
                                    "edrpou":           edrpou or None,
                                    "value_uah":        value_uah,
                                    "procuring_entity": pe_name,
                                    "tender_title":     tender_title,
                                    "tender_id":        tender.get("id", ""),
                                })
                                if len(preview_rows) >= preview:
                                    print(f"\n  Preview: first {preview} ProZorro suppliers found\n")
                                    for i, row in enumerate(preview_rows, 1):
                                        print(f"  [{i}] {row['name']}")
                                        print(f"       latin:     {row['name_latin']}")
                                        print(f"       edrpou:    {row['edrpou']}")
                                        print(f"       value_uah: {row['value_uah']:,.0f}")
                                        print(f"       buyer:     {row['procuring_entity']}")
                                        print(f"       tender:    {row['tender_title'][:80]}")
                                        print()
                                    return {
                                        "inserted": 0, "updated": 0, "skipped": 0,
                                        "total_list_items": total_list_items,
                                        "total_processed": total_processed,
                                        "total_cpv_passed": total_cpv_passed,
                                        "total_found": total_found,
                                        "cpv_misses": cpv_misses, "timed_out": False,
                                    }
                            else:
                                _, action = _upsert_supplier(
                                    conn,
                                    name=name,
                                    website=None,
                                    edrpou=edrpou or None,
                                    value_uah=value_uah,
                                    procuring_entity=pe_name,
                                    name_latin=name_latin,
                                )
                                if action == "inserted":
                                    inserted += 1
                                elif action == "updated":
                                    updated += 1
                                else:
                                    skipped += 1

                if batch_start + 30 < len(qualifying_ids):
                    await asyncio.sleep(0.5)

            # ── Post-page stop conditions ──────────────────────────────────────

            # Forward: stop once we've gone back to already-processed territory
            if mode == "forward" and high_water and page_oldest_mod <= high_water[:10]:
                print(f"  Forward scan complete — reached high water ({high_water[:10]})")
                break

            # Backfill: time limit (checked after full page, so cursor is clean)
            if deadline and time.monotonic() >= deadline:
                next_cursor = list_data.get("next_page", {}).get("offset", "")
                _set_meta(conn, _META_BFC, next_cursor)
                _set_meta(conn, _META_LWM, page_oldest_mod)
                elapsed = int(time.monotonic() - start_time)
                print(f"  Time limit reached ({elapsed}s) — progress saved at {page_oldest_mod}")
                timed_out = True
                break

            # Backfill: explicit target date
            if target_date and page_oldest_mod < target_date:
                _set_meta(conn, _META_BFC, "")
                _set_meta(conn, _META_LWM, target_date)
                print(f"  Target date {target_date} reached — backfill complete")
                break

            # Advance cursor
            nxt = list_data.get("next_page", {})
            offset = nxt.get("offset")
            if not offset:
                print("  Feed exhausted — pagination complete")
                if mode == "backfill":
                    _set_meta(conn, _META_BFC, "")
                break

            # Save backfill cursor after every page for clean resume
            if mode == "backfill":
                _set_meta(conn, _META_BFC, offset)
                _set_meta(conn, _META_LWM, page_oldest_mod)

    # ── Update watermarks ──────────────────────────────────────────────────────
    if session_newest and session_newest > high_water:
        _set_meta(conn, _META_HWM, session_newest)
    # On first-ever run (no low_water yet), seed it from this run's oldest page
    if session_oldest and not low_water:
        _set_meta(conn, _META_LWM, session_oldest)

    return {
        "inserted":         inserted,
        "updated":          updated,
        "skipped":          skipped,
        "total_list_items": total_list_items,
        "total_processed":  total_processed,
        "total_cpv_passed": total_cpv_passed,
        "total_found":      total_found,
        "cpv_misses":       cpv_misses,
        "timed_out":        timed_out,
    }


def scrape_prozorro(
    conn: sqlite3.Connection,
    mode: str = "forward",
    time_limit_min: int = 0,
    target_date: str = "",
    preview: int = 0,
) -> int:
    """
    Scrape ProZorro defense procurement tenders. Returns count of new rows inserted.

    mode="forward"  (default) — scan from present back to high_water_mark. Use for
                                daily/weekly pipeline runs. Fast once watermarks exist.
    mode="backfill"           — resume from backfill cursor, go further back in time.
                                Stops after time_limit_min minutes or target_date.

    progress is saved after every list page — safe to interrupt at any time.
    preview: if > 0, print first N suppliers and return without writing to DB.

    Run directly for a timed backfill:
        python src/ingest.py --backfill --minutes 45
    """
    _migrate_prozorro_cols(conn)
    _ensure_meta(conn)

    hwm = _get_meta(conn, _META_HWM) or "none"
    lwm = _get_meta(conn, _META_LWM) or "none"
    print(f"  Coverage before run:  {lwm}  →  {hwm}")

    start = time.monotonic()
    stats = asyncio.run(
        _scrape_prozorro_async(conn, mode, time_limit_min * 60, target_date, preview)
    )
    elapsed = time.monotonic() - start

    if preview == 0:
        hwm_after = _get_meta(conn, _META_HWM) or "none"
        lwm_after = _get_meta(conn, _META_LWM) or "none"

        print(f"\n  ProZorro {mode} stats")
        print(f"  {'─' * 44}")
        print(f"  Coverage after run:      {lwm_after}  →  {hwm_after}")
        print(f"  List items scanned:      {stats['total_list_items']:>8,}")
        print(f"  Defense tenders fetched: {stats['total_processed']:>8,}")
        print(f"  Passed CPV whitelist:    {stats['total_cpv_passed']:>8,}")
        print(f"  Suppliers this session:  {stats['total_found']:>8,}")
        print(f"  New rows inserted:       {stats['inserted']:>8,}")
        print(f"  Existing rows updated:   {stats['updated']:>8,}")
        print(f"  Total elapsed:           {elapsed:>7.1f}s")
        if stats["timed_out"]:
            print(f"  (time limit reached — resume with --backfill to continue)")

        total_in_db = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE source='prozorro'"
        ).fetchone()[0]
        print(f"  Total ProZorro companies in DB: {total_in_db}")

        top = conn.execute(
            """
            SELECT name, COALESCE(tender_count, 0), COALESCE(procurement_value, 0)
            FROM companies WHERE source = 'prozorro'
            ORDER BY tender_count DESC, procurement_value DESC
            LIMIT 10
            """
        ).fetchall()
        if top:
            print(f"\n  Top 10 by tender wins:")
            print(f"  {'#':<3} {'Company':<46} {'Wins':>5}  {'Value (UAH)':>15}")
            print(f"  {'─'*3} {'─'*46} {'─'*5}  {'─'*15}")
            for i, (name, tc, val) in enumerate(top, 1):
                print(f"  {i:<3} {name[:46]:<46} {tc:>5}  {val:>15,.0f}")

        if stats["total_found"] < 20 and stats["cpv_misses"]:
            print(f"\n  Only {stats['total_found']} companies found — CPV whitelist may be too tight.")
            print(f"  Sample of defense/complete tenders that FAILED CPV filter:\n")
            for i, m in enumerate(stats["cpv_misses"], 1):
                print(f"  [{i:>2}] CPV: {m['cpv']}")
                print(f"        Buyer: {m['buyer']}")
                print(f"        Title: {m['title']}")
                print()

    return stats["inserted"]


def seed_from_csv(conn: sqlite3.Connection) -> int:
    """Load companies from data/seed.csv (name, website columns). Returns count inserted."""
    if not SEED_CSV.exists():
        return 0

    inserted = 0
    with open(SEED_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row.get("name", "").strip()
            website = row.get("website", "").strip().rstrip("/") or None
            if name:
                inserted += _insert(conn, name, website, "seed")
    conn.commit()
    return inserted


# ── Pipeline orchestration ────────────────────────────────────────────────────

def run():
    conn = init_db()
    _migrate_pipeline_cols(conn)

    if SOURCES_CONFIG.get("brave1"):
        print("Scraping Brave1 cluster directory...")
        scraped = scrape_brave1(conn)
        print(f"  {scraped} new companies from Brave1")
    else:
        print("Brave1   — disabled (SOURCES_CONFIG['brave1'] = False)")

    if SOURCES_CONFIG.get("diana"):
        print("Scraping NATO DIANA 2026 cohort...")
        diana = scrape_diana(conn)
        print(f"  {diana} new companies from DIANA")
    else:
        print("DIANA    — disabled (SOURCES_CONFIG['diana'] = False)")

    if SOURCES_CONFIG.get("prozorro"):
        print("Scraping ProZorro defense procurement (forward mode)...")
        pz = scrape_prozorro(conn, mode="forward")
        print(f"  {pz} new companies from ProZorro")
    else:
        print("ProZorro — disabled (SOURCES_CONFIG['prozorro'] = False)")

    if SOURCES_CONFIG.get("articles"):
        print("Harvesting article sources...")
        arts = scrape_articles(conn)
        print(f"  {arts} new companies from article harvest")
    else:
        print("Articles — disabled (SOURCES_CONFIG['articles'] = False)")

    print("Loading seed CSV (data/seed.csv)...")
    seeded = seed_from_csv(conn)
    print(f"  {seeded} new companies from seed")

    by_source = conn.execute(
        "SELECT source, COUNT(*) FROM companies GROUP BY source ORDER BY source"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    print(f"Total companies in DB: {total}")
    for src, count in by_source:
        print(f"  {src or 'unknown'}: {count}")
    conn.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="UA Pipeline ingest")
    ap.add_argument("--backfill",  action="store_true",
                    help="Run ProZorro in backfill mode (go deeper into history)")
    ap.add_argument("--minutes",   type=int, default=45,
                    help="Time limit for backfill in minutes (default: 45)")
    ap.add_argument("--until",     default="",
                    help="Stop backfill when dateModified < YYYY-MM-DD")
    args = ap.parse_args()

    if args.backfill:
        conn = init_db()
        _migrate_prozorro_cols(conn)
        _ensure_meta(conn)
        print(f"ProZorro backfill — up to {args.minutes} min"
              + (f", target {args.until}" if args.until else ""))
        scrape_prozorro(conn, mode="backfill",
                        time_limit_min=args.minutes, target_date=args.until)
        conn.close()
    else:
        run()
