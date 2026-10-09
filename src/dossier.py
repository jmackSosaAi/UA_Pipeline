"""
Company Dossier Pipeline

Generates deep, multi-source investment dossiers for the top-N companies by score.

Steps per company:
  1. URL discovery  — homepage + article source_urls + 3 DuckDuckGo search queries
  2. Multi-source extraction — fetch up to 5 URLs, run Claude sonnet forced tool_use
  3. Score refresh  — reset scored_at so scorer.run() re-scores with richer fields
  4. Markdown summary — stored in DB and written to output/dossiers/<slug>.md

Run:
    python src/dossier.py --top 10
    python src/dossier.py --top 5 --force-refresh
    python src/dossier.py --company "Example Robotics"
"""

import json
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

import landscape  # noqa: E402
import score as scorer  # noqa: E402
import thesis  # noqa: E402
from collectors.base import fetch_page_text  # noqa: E402
from collectors.vocabulary import get_search_queries, map_category_to_sector  # noqa: E402
from db.migrate import migrate as migrate_database  # noqa: E402

DB_PATH   = Path(__file__).parent.parent / "data" / "companies.db"
OUT_DIR   = Path(__file__).parent.parent / "output" / "dossiers"
MEMO_DIR  = Path(__file__).parent.parent / "output" / "memos"

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
_MAX_PAGE_CHARS    = 10_000   # per URL, keep cost reasonable
_MAX_URLS_TO_FETCH = 5        # cap per company
# Note: _FETCH_TIMEOUT removed — _fetch_page_text now delegates to collectors.base.fetch_page_text
_FETCH_TIMEOUT    = 20
_DDG_DELAY        = 1.5      # seconds between DuckDuckGo requests (rate-limit-friendly)

_EXTRACTION_MODEL = "claude-sonnet-4-6"   # swap to claude-opus-4-7 for higher quality

# ── DB migration ─────────────────────────────────────────────────────────────

def _migrate_db(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, col_type in [
        ("dossier",         "TEXT"),   # JSON blob from extraction
        ("dossier_summary", "TEXT"),   # Markdown narrative
        ("dossier_at",      "TEXT"),   # datetime; NULL = not yet run
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS company_urls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id  INTEGER NOT NULL REFERENCES companies(id),
            url         TEXT NOT NULL,
            source      TEXT NOT NULL,  -- 'homepage' | 'article' | 'search'
            fetched_at  TEXT,
            UNIQUE(company_id, url)
        )
    """)

    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if "memo" not in existing:
        conn.execute("ALTER TABLE companies ADD COLUMN memo TEXT")

    conn.commit()


# ── Step 1: URL discovery ─────────────────────────────────────────────────────

_DDG_URL = "https://html.duckduckgo.com/html/"

def _ddg_search(query: str) -> list[str]:
    """Return up to 5 result URLs from a DuckDuckGo HTML search."""
    try:
        resp = requests.post(
            _DDG_URL,
            data={"q": query},
            headers={**_FETCH_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    urls = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        # DDG wraps URLs: /l/?uddg=<encoded-url>&...
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                try:
                    real = urllib.parse.unquote(m.group(1))
                    if real.startswith("http"):
                        urls.append(real)
                except Exception:
                    pass
        elif href.startswith("http"):
            urls.append(href)
        if len(urls) >= 5:
            break
    return urls


def _discover_urls(row: dict) -> list[tuple[str, str]]:
    """
    Return [(url, source)] list for a company row.
    Sources: 'homepage', 'article', 'search'.
    Deduplicates. Capped at _MAX_URLS_TO_FETCH total.
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []

    def _add(url: str, source: str) -> None:
        url = (url or "").strip().rstrip("/")
        if url and url not in seen and len(result) < _MAX_URLS_TO_FETCH:
            seen.add(url)
            result.append((url, source))

    # 1. Known homepage
    if row.get("website"):
        _add(row["website"], "homepage")

    # 2. Article source_urls already stored from brave1 harvester
    for url in json.loads(row.get("source_urls") or "[]"):
        _add(url, "article")

    # 3. DuckDuckGo searches (only if we still have room)
    if len(result) < _MAX_URLS_TO_FETCH:
        name = row["name"]

        # Look up sector from raw_leads category_hint (best-effort; falls back to None)
        sector_hint = map_category_to_sector(row.get("category_hint"))

        queries = get_search_queries(name, sector_hint=sector_hint)

        # For ProZorro companies (Cyrillic name stored, Latin transliteration in name_latin),
        # append Ukrainian-language queries to surface local defence media.
        if row.get("name_latin"):
            queries.append(f'"{name}" оборона')          # Cyrillic name + "defense"
        else:
            queries.append(f'"{name}" Україна оборона')  # Latin name + Ukraine defense

        for q in queries:
            if len(result) >= _MAX_URLS_TO_FETCH:
                break
            urls = _ddg_search(q)
            for u in urls:
                _add(u, "search")
                if len(result) >= _MAX_URLS_TO_FETCH:
                    break
            time.sleep(_DDG_DELAY)

    return result


def _save_urls(conn: sqlite3.Connection, company_id: int, urls: list[tuple[str, str]]) -> None:
    for url, source in urls:
        conn.execute(
            "INSERT OR IGNORE INTO company_urls (company_id, url, source) VALUES (?, ?, ?)",
            (company_id, url, source),
        )
    conn.commit()


# ── Step 2: Multi-source fetch + Claude extraction ────────────────────────────

_EXTRACT_TOOL = {
    "name": "extract_dossier",
    "description": (
        "Extract comprehensive investment-relevant information about a "
        "defense/technology company from multiple web sources."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            # Company overview
            "company_overview": {
                "type": "object",
                "properties": {
                    "description":        {"type": "string",
                                           "description": "2-3 sentence plain-English summary."},
                    "hq_country":         {"type": "string"},
                    "hq_city":            {"type": ["string", "null"]},
                    "founded_year":       {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "employee_count_est": {"type": ["string", "null"],
                                          "description": "e.g. '10-50', '50-200', or null"},
                    "legal_entity_country": {"type": ["string", "null"],
                                             "description": "Country of legal incorporation if different from HQ"},
                },
                "required": ["description", "hq_country"],
            },
            # Products and technology
            "product_technology": {
                "type": "object",
                "properties": {
                    "product_types":    {"type": "array", "items": {"type": "string"},
                                         "description": "e.g. ['UAV', 'counter-drone', 'EW system']"},
                    "notable_products": {"type": "array", "items": {"type": "string"},
                                         "description": "Named product models"},
                    "technologies":     {"type": "array", "items": {"type": "string"},
                                         "description": "Key technologies: AI, GNSS-denied nav, EO/IR, etc."},
                    "primary_sector":   {"type": "string", "enum": ["defense", "dual-use", "civilian"]},
                    "combat_deployed":  {"type": "boolean",
                                         "description": "Any product actively used in combat"},
                    "nato_customers":   {"type": "boolean",
                                         "description": "Any confirmed NATO-country customers"},
                },
                "required": ["product_types", "technologies", "primary_sector"],
            },
            # Traction & validation
            "traction_validation": {
                "type": "object",
                "properties": {
                    "contracts_mentioned": {"type": "array", "items": {"type": "string"},
                                             "description": "Specific contracts or procurement deals"},
                    "military_customers":  {"type": "array", "items": {"type": "string"},
                                             "description": "Named military units or agencies"},
                    "certifications":      {"type": "array", "items": {"type": "string"}},
                    "awards_or_grants":    {"type": "array", "items": {"type": "string"}},
                    "brave1_status":       {"type": ["string", "null"],
                                            "description": "Brave1 cluster/grant status if mentioned"},
                },
                "required": ["contracts_mentioned", "military_customers"],
            },
            # Financials & funding
            "financials_funding": {
                "type": "object",
                "properties": {
                    "total_raised_usd":    {"anyOf": [{"type": "number"}, {"type": "null"}],
                                            "description": "Total funding raised in USD"},
                    "last_round_type":     {"type": ["string", "null"],
                                            "description": "e.g. 'pre-seed', 'seed', 'Series A'"},
                    "last_round_amount_usd": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "last_round_date":     {"type": ["string", "null"], "description": "YYYY or YYYY-MM"},
                    "investors":           {"type": "array", "items": {"type": "string"}},
                    "revenue_mentioned":   {"type": "boolean"},
                    "revenue_detail":      {"type": ["string", "null"]},
                },
                "required": ["total_raised_usd", "investors"],
            },
            # Team
            "team": {
                "type": "object",
                "properties": {
                    "founders": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "name":  {"type": "string"},
                            "role":  {"type": "string"},
                            "background": {"type": ["string", "null"]},
                        },
                        "required": ["name", "role"],
                    }},
                    "technical_founders_count": {"type": "integer",
                                                  "description": "Number of founders with technical background"},
                    "military_veterans_on_team": {"type": "boolean"},
                },
                "required": ["founders", "technical_founders_count"],
            },
            # Strategic fit (analyst's read)
            "strategic_fit": {
                "type": "object",
                "properties": {
                    "post_war_durability": {"type": "string",
                                            "enum": ["high", "medium", "low", "unknown"],
                                            "description": "Revenue likely to survive end of active conflict?"},
                    "nato_export_path":    {"type": "string",
                                            "enum": ["clear", "possible", "unclear", "none"],
                                            "description": "Structural path to sell into NATO markets"},
                    "portfolio_overlap": {"type": "array", "items": {"type": "string"},
                                             "description": "Portfolio companies this competes with"},
                    "differentiators":     {"type": "array", "items": {"type": "string"},
                                            "description": "Key competitive differentiators vs alternatives"},
                    "key_risks":           {"type": "array", "items": {"type": "string"},
                                            "description": "Top 3 investment risks"},
                },
                "required": ["post_war_durability", "nato_export_path", "key_risks"],
            },
            # Press & sources
            "press_mentions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "headline":    {"type": "string"},
                        "publication": {"type": "string"},
                        "date":        {"type": ["string", "null"]},
                    },
                    "required": ["headline", "publication"],
                },
                "description": "Notable press coverage found across sources",
            },
        },
        "required": [
            "company_overview", "product_technology", "traction_validation",
            "financials_funding", "team", "strategic_fit", "press_mentions",
        ],
    },
}

_fund = thesis.fund()

_SYSTEM = (
    f"You are a senior investment analyst at {_fund['name']} ({_fund['mission']}), "
    f"a defense-tech venture fund focused on "
    f"{', '.join(_fund['geographic_focus']['tier_1'] + _fund['geographic_focus']['tier_2'])} "
    f"and other NATO-aligned defense companies at the pre-seed and seed stage. "
    "You are building a detailed investment dossier from multiple web sources. "
    "Extract only information that is explicitly stated or strongly implied across the sources. "
    "Never fabricate details. Use null or empty arrays for unknown fields. "
    "When sources disagree, prefer the most recent or most specific data. "
    "For strategic_fit fields, apply your own analytical judgment based on the evidence. "
    "The source text may be in Ukrainian. Extract all information into English regardless "
    "of the source language. Translate company descriptions, product names, and any other "
    "relevant text into English."
)


def _fetch_page_text(url: str) -> str | None:
    """Fetch a URL via Jina Reader (JS-safe) with BeautifulSoup fallback."""
    return fetch_page_text(url, max_chars=_MAX_PAGE_CHARS)


def _build_extraction_message(company_name: str, sources: list[tuple[str, str]]) -> str:
    """
    Build the user message for Claude: include text from all fetched URLs,
    labelled by source type.
    """
    parts = [f"Company: {company_name}\n"]
    for url, source, text in sources:
        parts.append(f"--- Source ({source}): {url} ---\n{text}\n")
    return "\n".join(parts)


def _extract_dossier(client: anthropic.Anthropic, company_name: str,
                     sources: list[tuple[str, str, str]]) -> dict | None:
    """
    Call Claude sonnet with forced tool_use to extract a full dossier.
    `sources` is [(url, source_type, text), ...].
    """
    message = _build_extraction_message(company_name, sources)
    try:
        resp = client.messages.create(
            model=_EXTRACTION_MODEL,
            max_tokens=4096,
            system=[{
                "type": "text",
                "text": _SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[_EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "extract_dossier"},
            messages=[{"role": "user", "content": message}],
        )
    except anthropic.APIError as exc:
        print(f"      Claude API error: {exc}")
        return None

    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    return None


# ── Step 3: Score refresh ─────────────────────────────────────────────────────

def _refresh_enrichment(conn: sqlite3.Connection, company_id: int, dossier: dict) -> None:
    """
    Push the richer dossier fields back into the standard enrichment columns
    so the scorer uses updated data.
    """
    overview    = dossier.get("company_overview", {})
    prod_tech   = dossier.get("product_technology", {})

    conn.execute(
        """UPDATE companies SET
               description        = COALESCE(?, description),
               product_types      = ?,
               primary_sector     = COALESCE(?, primary_sector),
               hq_country         = COALESCE(?, hq_country),
               founded_year       = COALESCE(?, founded_year),
               employee_count_est = COALESCE(?, employee_count_est),
               technologies       = ?,
               notable_products   = ?,
               enriched_at        = COALESCE(enriched_at, datetime('now')),
               enrich_error       = NULL,
               scored_at          = NULL
           WHERE id = ?""",
        (
            overview.get("description"),
            json.dumps(prod_tech.get("product_types") or []),
            prod_tech.get("primary_sector"),
            overview.get("hq_country"),
            overview.get("founded_year"),
            overview.get("employee_count_est"),
            json.dumps(prod_tech.get("technologies") or []),
            json.dumps(prod_tech.get("notable_products") or []),
            company_id,
        ),
    )
    conn.commit()


# ── Step 4: Markdown summary ──────────────────────────────────────────────────

_SUMMARY_PROMPT_TEMPLATE = """\
Company: {name}
Score: {score:.2f} / 3.00
Website: {website}

--- DOSSIER DATA ---
{dossier_json}
--- END DOSSIER DATA ---

Write a 4–6 paragraph investment dossier memo in this exact structure:

**OVERVIEW**
2-3 sentences: what the company builds, who uses it, and why it matters to \
{fund_name}'s thesis.

**PRODUCT & TECHNOLOGY**
Specific products and technologies. Name actual products where known. \
Call out combat deployment, Brave1 status, or NATO customers explicitly.

**TRACTION & VALIDATION**
Concrete evidence of product-market fit: named military customers, contracts, \
awards, Brave1 grants, revenue signals. Be specific or say it's not yet evidenced.

**TEAM**
Founder names, roles, and backgrounds. Explicitly flag whether technical \
co-founders are present (Non-negotiable). Military veterans on team?

**INVESTMENT THESIS FIT**
Map directly to the fund's six dimensions: defense relevance, technical founders, \
post-war durability, NATO exportability, stage fit, shipped product. \
Call out sector bonuses (counter-drone, autonomous interception, robotics, autonomy software).
Note any overlap with portfolio companies.

**KEY RISKS**
Top 3 investment risks. Be direct — no hedging.

Guidelines:
- Do not start paragraphs with the company name or "This company"
- Be specific; avoid generic VC language ("innovative solution", "cutting-edge")
- Use numbers and named entities wherever the data supports it
- If a field is unknown, say so directly rather than omitting it
"""


def _generate_markdown_summary(
    client: anthropic.Anthropic,
    row: dict,
    dossier: dict,
) -> str:
    prompt = _SUMMARY_PROMPT_TEMPLATE.format(
        name=row["name"],
        score=float(row.get("total_score") or 0),
        website=row.get("website") or "—",
        dossier_json=json.dumps(dossier, indent=2),
        fund_name=_fund["name"],
    )
    try:
        resp = client.messages.create(
            model=_EXTRACTION_MODEL,
            max_tokens=2048,
            system=[{
                "type": "text",
                "text": _SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        return next(
            (b.text.strip() for b in resp.content if b.type == "text"), ""
        )
    except anthropic.APIError as exc:
        return f"[summary generation failed: {exc}]"


def _save_dossier(conn: sqlite3.Connection, company_id: int,
                  dossier: dict, summary: str) -> None:
    conn.execute(
        """UPDATE companies
           SET dossier = ?, dossier_summary = ?, dossier_at = datetime('now')
           WHERE id = ?""",
        (json.dumps(dossier), summary, company_id),
    )
    conn.commit()


def _write_markdown_file(row: dict, summary: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\-]", "_", row["name"].lower())
    path = OUT_DIR / f"{slug}.md"
    score_str = f"{float(row.get('total_score') or 0):.2f}"
    header = (
        f"# {row['name']}\n\n"
        f"**Score:** {score_str} / 3.00  \n"
        f"**Website:** {row.get('website') or '—'}  \n"
        f"**Country:** {row.get('hq_country') or 'unknown'}  \n"
        f"**Generated:** {__import__('datetime').date.today()}\n\n"
        "---\n\n"
    )
    path.write_text(header + summary, encoding="utf-8")
    return path


# ── Memo generation ──────────────────────────────────────────────────────────

_MEMO_MODEL = "claude-sonnet-4-6"

_MEMO_SYSTEM = (
    f"You are a senior investment analyst at {_fund['name']} ({_fund['mission']}). "
    "Write crisp, direct investment memos — no hedging, no filler, specific claims only. "
    "Use numbers and named entities wherever the data supports it. "
    "If a field is unknown, state it directly rather than omitting it."
)

_MEMO_PROMPT_TEMPLATE = """\
Prepare a one-page investment memo for Example Fund.

Company: {name}
Score: {score:.2f} / 3.00
Status recommendation: {status_label}
Website: {website}

--- DOSSIER DATA ---
{dossier_json}
--- END DOSSIER DATA ---

--- COMPETITIVE CONTEXT (same category: {category}) ---
{peers_text}
--- END COMPETITIVE CONTEXT ---

Write the memo in exactly this format. Do not add or remove sections.

# {name} — Investment Memo Draft
**Prepared for:** Example Fund
**Date:** {today}
**Status:** {status_label}

## Summary
One paragraph: what they build, who uses it, what stage they're at, \
and why this is timely for the fund's thesis.

## Product & Traction
Key products by name, deployment status, named military customers or contracts, \
Brave1/grant status. Be specific or state the data is absent.

## Market & Timing
Why now? Reference specific timing factors: Ukraine conflict dynamics, \
NATO procurement cycles, relevant thesis priorities \
(counter-drone, autonomous interception, robotics at the zero line, autonomy software).

## Team Assessment
Named founders with roles and backgrounds. \
Explicitly state whether the fund's non-negotiable — at least one technical co-founder — is met. \
Military veterans on team?

## Strategic Fit
Map to the fund's six scoring dimensions with one sentence each. \
Name any portfolio companies this complements or competes with. \
Which of the fund's value-adds apply?

## Competitive Landscape
Compare briefly against the {peer_count} most similar companies in the database. \
For each: name, score, one-sentence differentiation relative to this company.

## Key Risks
2–3 specific concerns. No generic VC risks ("execution risk", "market risk"). \
Be direct about what could kill this investment.

## Recommendation
One of: "Request deck and schedule intro call" / "Monitor for 6 months, \
revisit at [milestone]" / "Pass — [specific reason]". \
Include the specific next step.
"""


def generate_memo(
    client: anthropic.Anthropic,
    conn: sqlite3.Connection,
    company_id: int,
    force_refresh: bool = False,
) -> str | None:
    """
    Generate a one-page investment memo for a company that has a completed dossier.
    Caches the result in the `memo` column and writes output/memos/{slug}.md.
    Returns the memo text, or None if the company has no dossier.
    """
    row = dict(
        conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    )

    if not row.get("dossier"):
        print(f"  ⚠  {row['name']} has no dossier — run dossier pipeline first")
        return None

    if row.get("memo") and not force_refresh:
        print(f"  ↩  {row['name']} — memo already exists (use force_refresh=True to redo)")
        return row["memo"]

    dossier = json.loads(row["dossier"])
    score = float(row.get("total_score") or 0)

    if score >= 2.5:
        status_label = "Recommend"
    elif score >= 1.8:
        status_label = "Watch"
    else:
        status_label = "Pass"

    peers = landscape.similar_companies(company_id, top_n=5, conn=conn)
    if peers:
        peer_lines = []
        for p in peers:
            signals = ", ".join(p["traction_signals"]) or "no signals"
            peer_lines.append(
                f"- {p['name']} (score {p['total_score']:.2f}, "
                f"T{p['tier'] or '?'}) — {(p['description'] or '')[:120]}"
            )
        peers_text = "\n".join(peer_lines)
    else:
        peers_text = "No other companies in same category yet."

    prompt = _MEMO_PROMPT_TEMPLATE.format(
        name=row["name"],
        score=score,
        status_label=status_label,
        website=row.get("website") or "—",
        dossier_json=json.dumps(dossier, indent=2),
        category=row.get("primary_category") or "Unknown",
        peers_text=peers_text,
        peer_count=len(peers),
        today=__import__("datetime").date.today(),
    )

    try:
        resp = client.messages.create(
            model=_MEMO_MODEL,
            max_tokens=3000,
            system=[{
                "type": "text",
                "text": _MEMO_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        memo = next((b.text.strip() for b in resp.content if b.type == "text"), "")
    except anthropic.APIError as exc:
        print(f"      memo generation failed: {exc}")
        return None

    conn.execute(
        "UPDATE companies SET memo = ? WHERE id = ?",
        (memo, company_id),
    )
    conn.commit()

    MEMO_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\-]", "_", row["name"].lower())
    path = MEMO_DIR / f"{slug}.md"
    path.write_text(memo, encoding="utf-8")
    print(f"  Memo written → {path}")

    return memo


# ── Main orchestrator ─────────────────────────────────────────────────────────

def _load_candidates(conn: sqlite3.Connection, top_n: int,
                     company_names: list[str] | None, force_refresh: bool) -> list[dict]:
    if company_names:
        # Named lookup: no scored_at requirement — dossier itself will enrich + score.
        # Case-insensitive LIKE match so "M-fly" finds "M-Fly", etc.
        rows = []
        for name in company_names:
            found = conn.execute(
                "SELECT * FROM companies "
                "WHERE LOWER(name) = LOWER(?) "
                "AND (portfolio_company IS NULL OR portfolio_company = 0)",
                (name,),
            ).fetchall()
            if not found:
                print(f"  ⚠  '{name}' not found in database — skipping")
            rows.extend(found)
    else:
        rows = conn.execute(
            """SELECT * FROM companies
               WHERE scored_at IS NOT NULL AND enrich_error IS NULL
                 AND (portfolio_company IS NULL OR portfolio_company = 0)
               ORDER BY total_score DESC
               LIMIT ?""",
            (top_n,),
        ).fetchall()

    result = []
    for row in rows:
        r = dict(row)
        if not force_refresh and r.get("dossier_at"):
            print(f"  ↩  {r['name']} — dossier already exists (use --force-refresh to redo)")
            continue
        result.append(r)
    return result


def run(
    top_n: int = 10,
    company_names: list[str] | None = None,
    force_refresh: bool = False,
) -> None:
    migrate_database(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _migrate_db(conn)

    candidates = _load_candidates(conn, top_n, company_names, force_refresh)
    if not candidates:
        if company_names:
            print("No new dossiers to generate (all already exist or not found).")
        else:
            print(f"All top-{top_n} companies already have dossiers "
                  "(use --force-refresh to regenerate).")
        conn.close()
        return

    print(f"Generating dossiers for {len(candidates)} companies...\n")
    client = anthropic.Anthropic()
    total  = len(candidates)

    for i, row in enumerate(candidates, 1):
        name = row["name"]
        print(f"[{i}/{total}] {name}")

        # ── Step 1: URL discovery ─────────────────────────────────────────
        print("  Step 1: discovering URLs...")
        url_sources = _discover_urls(row)
        _save_urls(conn, row["id"], url_sources)
        print(f"    {len(url_sources)} URLs discovered: "
              + ", ".join(s for _, s in url_sources))

        # ── Step 2: Fetch + extract ───────────────────────────────────────
        print("  Step 2: fetching pages and extracting dossier...")
        fetched: list[tuple[str, str, str]] = []
        for url, source in url_sources:
            text = _fetch_page_text(url)
            if text:
                fetched.append((url, source, text))
                print(f"    {source}: {len(text):,} chars — {url[:70]}")
            time.sleep(0.5)

        if not fetched:
            print("    No pages fetched — skipping this company.")
            continue

        dossier = _extract_dossier(client, name, fetched)
        if not dossier:
            print("    Extraction failed — skipping.")
            continue

        fin = dossier.get("financials_funding", {})
        team = dossier.get("team", {})
        print(
            f"    Extracted: "
            f"raised=${fin.get('total_raised_usd') or 'unknown'} | "
            f"founders={len(team.get('founders', []))} | "
            f"tech_founders={team.get('technical_founders_count', '?')}"
        )

        # ── Step 3: Refresh enrichment + rescore ─────────────────────────
        print("  Step 3: refreshing enrichment and rescoring...")
        _refresh_enrichment(conn, row["id"], dossier)
        scorer.run()

        # Reload the row to get updated score
        updated_row = dict(
            conn.execute(
                "SELECT * FROM companies WHERE id = ?", (row["id"],)
            ).fetchone()
        )

        # ── Step 4: Generate markdown summary ────────────────────────────
        print("  Step 4: generating narrative summary...")
        summary = _generate_markdown_summary(client, updated_row, dossier)
        _save_dossier(conn, row["id"], dossier, summary)

        md_path = _write_markdown_file(updated_row, summary)
        old_score = float(row.get("total_score") or 0)
        new_score = float(updated_row.get("total_score") or 0)
        delta = f"+{new_score - old_score:.2f}" if new_score >= old_score else f"{new_score - old_score:.2f}"
        print(f"  Done: score {old_score:.2f} → {new_score:.2f} ({delta}) | {md_path}")
        print()

        time.sleep(1.0)

    conn.close()
    print(f"Dossiers written to {OUT_DIR}/")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Generate investment dossiers")
    ap.add_argument("--top",           type=int,  default=10,
                    help="Generate dossiers for top-N companies by score (default: 10)")
    ap.add_argument("--company",       type=str,  nargs="+", default=None, metavar="NAME",
                    help="One or more company names  e.g. --company Tencore Teletactica")
    ap.add_argument("--force-refresh", action="store_true",
                    help="Regenerate dossiers even if they already exist")
    args = ap.parse_args()

    run(
        top_n=args.top,
        company_names=args.company,
        force_refresh=args.force_refresh,
    )
