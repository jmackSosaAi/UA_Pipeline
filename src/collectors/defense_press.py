"""
Defense press collector — automated company discovery from defense industry RSS feeds.

For each feed listed in FEEDS:
  1. Pull recent items via feedparser.
  2. Skip articles already in processed_articles.
  3. Fetch full article text (Jina Reader → raw fallback).
  4. Keyword-filter against search_vocabulary.yaml; skip articles with zero hits.
  5. Send surviving articles to Claude → summary, relevance_score, sector_tags,
     and a list of named companies.
  6. Insert the article into processed_articles, store each named company as a
     raw lead, then link the lead's canonical to the article via article_companies.
  7. After the run, backfill article_companies.company_id from canonical → companies.

Run:
    python -m src.collectors.defense_press
    python -m src.collectors.defense_press --limit 3 --feed "Breaking Defense"
"""

import argparse
import json
import sqlite3
import sys
import time

import anthropic
import feedparser
from dotenv import load_dotenv

try:
    from db.migrate import migrate as migrate_database
except ImportError:  # supports `python -m src.collectors.defense_press`
    from src.db.migrate import migrate as migrate_database

from .base import DB_PATH, fetch_page_text, store_lead
from .dedup import find_exact_match, normalize_name
from .source_config import (
    load_excluded_companies,
    load_noise_description_patterns,
    load_rss_feeds,
)
from .vocabulary import get_all_sector_keys, get_sector_terms, load_vocabulary

load_dotenv()

SOURCE = "Defense Press"

# Feeds + excluded primes live in config/sources/*.yaml.
FEEDS: dict[str, str] = load_rss_feeds()
_EXCLUDED_SET: set[str] = load_excluded_companies()


def _is_excluded(name: str) -> bool:
    """Return True if `name` is an excluded prime/mega-cap.

    Matches in two ways:
      1. Exact (case-insensitive) match against EXCLUDED_COMPANIES.
      2. Subsidiary prefix: `name` starts with an excluded entry plus a space
         (e.g. "Boeing Defense", "General Dynamics Electric Boat"). Skipped
         for short entries (len ≤ 5) so abbreviations like HII/GD/NG/BAE
         and very short brands like "RTX"/"Saab" don't swallow unrelated names.
    """
    lower = name.strip().lower()
    if not lower:
        return False
    if lower in _EXCLUDED_SET:
        return True
    for excluded in _EXCLUDED_SET:
        if len(excluded) > 5 and lower.startswith(excluded + " "):
            return True
    return False


# ---------------------------------------------------------------------------
# Defense-relevance gate
# ---------------------------------------------------------------------------


import re

_VOCAB_TERMS_CACHE: list[str] | None = None
_VOCAB_SHORT_RE: "re.Pattern | None" = None
_VOCAB_LONG_TERMS: list[str] | None = None


def _vocab_terms_lower() -> list[str]:
    """Lazy single-build of the lowercased vocab list (sector + general modifiers)."""
    global _VOCAB_TERMS_CACHE
    if _VOCAB_TERMS_CACHE is None:
        terms: set[str] = set()
        for key in get_all_sector_keys():
            for t in get_sector_terms(key):
                if t:
                    terms.add(t.strip().lower())
        for t in load_vocabulary().get("general_modifiers", []) or []:
            if t:
                terms.add(t.strip().lower())
        _VOCAB_TERMS_CACHE = sorted(terms, key=len, reverse=True)
    return _VOCAB_TERMS_CACHE


def _vocab_match_structures() -> tuple["re.Pattern | None", list[str]]:
    """Return (short_term_regex, long_terms) — split for performant matching.

    Short tokens (≤ 4 chars) are matched with \b…\b word boundaries so e.g.
    'ew' (electronic-warfare abbreviation) doesn't fire on 'n**ew**s outlet'.
    Longer multi-word phrases ('seed round', 'national security') stay on
    plain substring matching.
    """
    global _VOCAB_SHORT_RE, _VOCAB_LONG_TERMS
    if _VOCAB_SHORT_RE is None or _VOCAB_LONG_TERMS is None:
        terms = _vocab_terms_lower()
        short = [t for t in terms if len(t) <= 4]
        long_ = [t for t in terms if len(t) > 4]
        if short:
            pattern = r"\b(?:" + "|".join(re.escape(t) for t in short) + r")\b"
            _VOCAB_SHORT_RE = re.compile(pattern)
        else:
            _VOCAB_SHORT_RE = re.compile(r"(?!x)x")  # never-matches sentinel
        _VOCAB_LONG_TERMS = long_
    return _VOCAB_SHORT_RE, _VOCAB_LONG_TERMS


def is_defense_relevant(name: str | None, description: str | None) -> bool:
    """Promotion gate: True if (name + description) reads as a real defense
    company that should enter canonical_companies → companies.

    Logic — the negative check wins:
      1. If `description` contains any noise pattern from
         excluded_companies.yaml::noise_description_patterns ("media outlet",
         "publisher", "sponsor", "consulting firm", etc.) → False, even if
         the same description mentions defense vocabulary.
      2. Else, if `name` OR `description` contains any defense vocabulary
         term (sector primary/search terms + general modifiers) → True.
      3. Else → False.

    article_companies mention links are recorded *regardless* of this
    function — only the promotion path is gated.
    """
    desc_l = (description or "").lower()
    name_l = (name or "").lower()

    # Negative gate first — wins over positive.
    for pattern in load_noise_description_patterns():
        if pattern and pattern in desc_l:
            return False

    # Positive gate.
    haystack = f"{name_l} {desc_l}"
    if not haystack.strip():
        return False
    short_re, long_terms = _vocab_match_structures()
    if short_re.search(haystack):
        return True
    for term in long_terms:
        if term in haystack:
            return True
    return False


# ---------------------------------------------------------------------------
# Self-test fixture (run via `python -m src.collectors.defense_press --self-test`)
# ---------------------------------------------------------------------------

_SELF_TEST_FIXTURES: list[tuple[str, str, bool]] = [
    # (name, description, expected_is_defense_relevant)
    # Clear noise — fail negative gate
    ("ABC News",
     "U.S. media outlet whose Chief Washington Correspondent spoke with Trump "
     "about Iranian missile and drone attacks.",
     False),
    ("Adobe",
     "Sponsor of an insights report titled 'From Paper to Pixels'.",
     False),
    ("Splunk",
     "Sponsor of an insights report on the digital domain.",
     False),
    ("RAND",
     "Defense research firm that simulated a congressional acquisition pathway.",
     False),
    ("The Artemis Group",
     "Consulting firm where Jim Bridenstine worked as a managing partner.",
     False),
    ("Government Media Executive Group LLC",
     "Parent company and publisher of Defense One.",
     False),

    # Real defense — pass positive gate
    ("AeroVironment, Inc.",
     "Defense contractor behind the LOCUST Laser Weapon System and Switchblade drones.",
     True),
    # Vocab-rich Rheinmetall description → passes.
    # Uses vocabulary phrases that actually exist in search_vocabulary.yaml
    # ('unmanned ground vehicle' matches under autonomous_ground sector).
    ("American Rheinmetall",
     "Competing to supply the new unmanned ground vehicle for the US Army; "
     "competitor to General Dynamics on the XM30 Bradley replacement.",
     True),
    # Sparse Rheinmetall description (matches the actual DB row text) →
    # honest gap: no vocab term in the blurb, gate returns False even though
    # this is a known prime. Cleanup rows like this stay un-filtered only
    # because they fail the negative gate too — they'll surface in the
    # dry-run's "ambiguous (filter divergence)" bucket.
    ("American Rheinmetall (sparse blurb)",
     "Competing to secure the XM30 Bradley replacement contract.",
     False),
    ("Shield AI",
     "Developer of the X-BAT autonomous VTOL stealth fighter drone.",
     True),
    ("Acecore Technologies",
     "Dutch drone manufacturer signed up for Intelic's BASE marketplace.",
     True),

    # Sneaky case: noise description that mentions defense vocab anyway —
    # negative gate wins.
    ("Defense Daily Sponsorships LLC",
     "Sponsored by Lockheed; covers drone, missile, and ISR programs.",
     False),

    # Edge: name alone is enough to pass when description is empty
    ("Drone Systems Inc.", "", True),

    # Edge: empty everything → False
    ("", "", False),
    ("Generic Co", "Civilian software for office productivity.", False),

    # Edge: ADNOC (oil company that mentions drones in its blurb) — current
    # filter intentionally LACKS an "oil company" / "tanker operator" pattern,
    # so this passes. Document the known gap as a test that asserts the
    # current behaviour, not the ideal behaviour.
    ("ADNOC",
     "Abu Dhabi National Oil Company whose tanker was attacked by Iranian drones.",
     True),
]


def _run_self_test() -> int:
    ok = fail = 0
    for name, desc, expected in _SELF_TEST_FIXTURES:
        got = is_defense_relevant(name, desc)
        mark = "✓" if got == expected else "✗"
        if got == expected:
            ok += 1
        else:
            fail += 1
        print(f"  {mark} is_defense_relevant({name[:30]!r:32s}, …) = {got!s:5s}  expected={expected}")
    print(f"\n{ok} passed, {fail} failed")
    return 0 if fail == 0 else 1

_INTER_REQUEST_SLEEP = 1.0          # gentle pacing between article fetches
_FULL_TEXT_PREVIEW_CHARS = 500      # stored in processed_articles.full_text_preview


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------


def _build_extract_tool() -> dict:
    """Tool schema. sector_tags is enum-constrained to current vocabulary keys."""
    sector_keys = get_all_sector_keys()
    return {
        "name": "extract_article",
        "description": (
            "Extract structured information about a defense industry article: a brief "
            "summary, a thesis-relevance score, the vocabulary sector keys it touches, and "
            "every defense / dual-use / military-tech company it names. Exclude "
            "government agencies, military units, and person names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "2-sentence summary of the article (max).",
                },
                "relevance_score": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": (
                        "0.0–1.0. How relevant to the fund's pre-seed/seed defense-tech "
                        "investment thesis. High = small / mid defense-tech companies; "
                        "low = mostly about primes or non-tech topics."
                    ),
                },
                "sector_tags": {
                    "type": "array",
                    "items": {"type": "string", "enum": sector_keys},
                    "description": "Vocabulary sector keys this article touches.",
                },
                "companies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Official company name as written in the article.",
                            },
                            "context": {
                                "type": "string",
                                "description": "Brief description of the company taken from the article.",
                            },
                        },
                        "required": ["name", "context"],
                    },
                },
            },
            "required": ["summary", "relevance_score", "sector_tags", "companies"],
        },
    }


_EXTRACT_TOOL = _build_extract_tool()

_SYSTEM = (
    "You are a defense-sector research analyst extracting structured data from "
    "defense industry news articles. Be precise: only extract companies (commercial "
    "entities — not government agencies, military units, or person names) that are "
    "explicitly named in the article. Do not hallucinate."
)

_CLIENT = anthropic.Anthropic()


def _extract_article(page_text: str, article_url: str) -> dict:
    """Ask Claude for summary + relevance + sector_tags + companies.

    Returns {summary, relevance_score, sector_tags, companies} — companies already
    filtered against EXCLUDED_COMPANIES. Returns an empty-ish dict on Claude error.
    """
    try:
        response = _CLIENT.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_SYSTEM,
            tools=[_EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "extract_article"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Article URL: {article_url}\n\n"
                        f"Article text:\n{page_text}"
                    ),
                }
            ],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "extract_article":
                payload = block.input or {}
                companies_in = payload.get("companies", []) or []
                kept = []
                for c in companies_in:
                    name = (c.get("name") or "").strip()
                    if not name:
                        continue
                    if _is_excluded(name):
                        print(f"      - {name} (excluded prime)")
                        continue
                    kept.append({"name": name, "context": (c.get("context") or "").strip()})
                return {
                    "summary": (payload.get("summary") or "").strip(),
                    "relevance_score": float(payload.get("relevance_score") or 0.0),
                    "sector_tags": list(payload.get("sector_tags") or []),
                    "companies": kept,
                }
    except Exception as e:
        print(f"  [claude error] {e}", file=sys.stderr)
    return {"summary": "", "relevance_score": 0.0, "sector_tags": [], "companies": []}


# ---------------------------------------------------------------------------
# Keyword filter
# ---------------------------------------------------------------------------


def _build_vocab_terms() -> list[str]:
    """All sector terms (primary + search) lowercased — for cheap substring matching."""
    terms: set[str] = set()
    for sector_key in get_all_sector_keys():
        for term in get_sector_terms(sector_key):
            terms.add(term.strip().lower())
    for term in load_vocabulary().get("general_modifiers", []):
        terms.add(term.strip().lower())
    return sorted(terms, key=len, reverse=True)


def _keyword_hits(text: str, terms: list[str]) -> int:
    """Count how many vocabulary terms appear (substring) in the article text."""
    lower = text.lower()
    return sum(1 for t in terms if t and t in lower)


# ---------------------------------------------------------------------------
# processed_articles + article_companies helpers
# ---------------------------------------------------------------------------


def _is_processed(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_articles WHERE url = ? LIMIT 1", (url,)
    ).fetchone()
    return row is not None


def _insert_article(
    conn: sqlite3.Connection,
    url: str,
    title: str | None,
    publication: str,
    published_at: str | None,
    summary: str | None,
    relevance_score: float | None,
    sector_tags: list[str] | None,
    full_text_preview: str | None,
    byline: str | None,
) -> int | None:
    """Insert a row into processed_articles. Returns its id (or None on duplicate)."""
    try:
        cursor = conn.execute(
            """
            INSERT INTO processed_articles
                (url, title, publication, published_at,
                 summary, relevance_score, sector_tags,
                 full_text_preview, byline, companies_found)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                url, title, publication, published_at,
                summary or None,
                relevance_score if relevance_score is not None else None,
                json.dumps(sector_tags) if sector_tags else None,
                full_text_preview or None,
                byline or None,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def _set_companies_found(conn: sqlite3.Connection, article_id: int, n: int) -> None:
    conn.execute(
        "UPDATE processed_articles SET companies_found = ? WHERE id = ?",
        (n, article_id),
    )
    conn.commit()


def _link_company_to_article(
    conn: sqlite3.Connection,
    article_id: int,
    company_name: str,
    context: str | None,
) -> None:
    """Insert into article_companies. Looks up canonical_id by normalized name."""
    canonical_id = find_exact_match(normalize_name(company_name), conn)
    conn.execute(
        """
        INSERT OR IGNORE INTO article_companies
            (article_id, canonical_id, company_name, context)
        VALUES (?, ?, ?, ?)
        """,
        (article_id, canonical_id, company_name, context),
    )
    conn.commit()


def _entry_published(entry) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", parsed)
    except Exception:
        return None


def _entry_author(entry) -> str | None:
    """Best-effort byline from a feedparser entry."""
    for key in ("author", "dc_creator", "creator"):
        val = entry.get(key)
        if val:
            return str(val).strip() or None
    authors = entry.get("authors")
    if authors:
        names = [a.get("name") for a in authors if a.get("name")]
        if names:
            return ", ".join(names)
    return None


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def backfill_company_ids() -> int:
    """Fill article_companies.company_id by joining canonical → companies (by name).

    `companies.name` is stored verbatim (mixed case, with suffixes); we apply
    the same normalization used by canonical_companies.canonical_name, then
    match. Only fills rows where company_id IS NULL and canonical_id IS NOT NULL.
    Returns the number of rows updated.
    """
    with sqlite3.connect(DB_PATH) as conn:
        # Build canonical_name → company_id lookup
        comp_by_canon: dict[str, int] = {}
        for cid, cname in conn.execute("SELECT id, name FROM companies").fetchall():
            if not cname:
                continue
            comp_by_canon.setdefault(normalize_name(cname), cid)

        rows = conn.execute("""
            SELECT ac.id, cc.canonical_name
              FROM article_companies ac
              JOIN canonical_companies cc ON ac.canonical_id = cc.id
             WHERE ac.company_id IS NULL
               AND ac.canonical_id IS NOT NULL
        """).fetchall()

        updated = 0
        for ac_id, canon_name in rows:
            comp_id = comp_by_canon.get(canon_name)
            if comp_id is not None:
                conn.execute(
                    "UPDATE article_companies SET company_id = ? WHERE id = ?",
                    (comp_id, ac_id),
                )
                updated += 1
        conn.commit()
    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(limit: int | None = None, feed_filter: str | None = None) -> None:
    if feed_filter and feed_filter not in FEEDS:
        valid = ", ".join(FEEDS)
        print(f"Unknown feed '{feed_filter}'. Valid: {valid}", file=sys.stderr)
        sys.exit(2)

    migrate_database(DB_PATH)

    feeds = {feed_filter: FEEDS[feed_filter]} if feed_filter else FEEDS

    vocab_terms = _build_vocab_terms()
    print(f"Loaded {len(vocab_terms)} vocabulary terms for keyword filter")

    feeds_checked = 0
    new_articles = 0
    companies_discovered = 0

    with sqlite3.connect(DB_PATH) as conn:
        for publication, feed_url in feeds.items():
            feeds_checked += 1
            print(f"\n=== {publication} — {feed_url} ===")
            parsed = feedparser.parse(feed_url)
            if parsed.bozo:
                print(f"  [warn] feed parse issue: {parsed.bozo_exception}", file=sys.stderr)
            entries = parsed.entries or []
            if limit:
                entries = entries[:limit]
            print(f"  {len(entries)} entries to consider")

            for i, entry in enumerate(entries, 1):
                url = (entry.get("link") or "").strip()
                title = (entry.get("title") or "").strip() or None
                if not url:
                    continue

                if _is_processed(conn, url):
                    print(f"  [{i}] skip (already processed): {url}")
                    continue

                print(f"  [{i}] {title or url}")
                page_text = fetch_page_text(url)
                if not page_text:
                    print("      [skip] fetch failed")
                    continue

                hits = _keyword_hits(page_text, vocab_terms)
                preview = page_text[:_FULL_TEXT_PREVIEW_CHARS]
                published_at = _entry_published(entry)
                byline = _entry_author(entry)

                if hits == 0:
                    print("      [skip] no vocabulary hits")
                    _insert_article(
                        conn, url, title, publication, published_at,
                        summary=None, relevance_score=None, sector_tags=None,
                        full_text_preview=preview, byline=byline,
                    )
                    new_articles += 1
                    continue

                print(f"      {hits} keyword hits — extracting article")
                extracted = _extract_article(page_text, url)
                summary = extracted["summary"]
                relevance_score = extracted["relevance_score"]
                sector_tags = extracted["sector_tags"]
                companies = extracted["companies"]

                if summary:
                    print(f"      summary: {summary[:120]}{'…' if len(summary) > 120 else ''}")
                print(f"      relevance: {relevance_score:.2f}  tags: {sector_tags}")

                article_id = _insert_article(
                    conn, url, title, publication, published_at,
                    summary=summary, relevance_score=relevance_score,
                    sector_tags=sector_tags,
                    full_text_preview=preview, byline=byline,
                )
                if article_id is None:
                    # Race / duplicate URL — already processed; skip linking.
                    print("      [skip] article already in DB")
                    continue

                stored_count = 0
                for company in companies:
                    name = (company.get("name") or "").strip()
                    if not name:
                        continue
                    context = (company.get("context") or "").strip() or None

                    # Gate the canonical_companies/companies promotion path.
                    # The article_companies link is recorded either way so press
                    # intelligence keeps the full mention graph.
                    promote = is_defense_relevant(name, context)

                    if promote:
                        new_lead = store_lead(
                            company_name=name,
                            source=SOURCE,
                            source_url=url,
                            initial_description=context,
                        )
                    else:
                        new_lead = None

                    _link_company_to_article(conn, article_id, name, context)

                    if not promote:
                        print(f"      ° {name} (filtered: not defense-relevant — mention recorded only)")
                    elif new_lead:
                        stored_count += 1
                        print(f"      + {name}")
                    else:
                        print(f"      ~ {name} (duplicate lead, linked)")

                companies_discovered += stored_count
                _set_companies_found(conn, article_id, stored_count)
                new_articles += 1
                time.sleep(_INTER_REQUEST_SLEEP)

    backfilled = backfill_company_ids()
    print(f"\nbackfill_company_ids: {backfilled} article_companies row(s) linked to companies")

    print("\n" + "=" * 60)
    print(
        f"{feeds_checked} feeds checked, {new_articles} new articles, "
        f"{companies_discovered} companies discovered"
    )
    print("=" * 60)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Defense press RSS collector")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only N articles per feed (for testing).",
    )
    p.add_argument(
        "--feed",
        type=str,
        default=None,
        help="Process only one feed by display name (e.g. \"Breaking Defense\").",
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="Run is_defense_relevant() against the built-in test fixtures and exit.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.self_test:
        sys.exit(_run_self_test())
    run(limit=args.limit, feed_filter=args.feed)
