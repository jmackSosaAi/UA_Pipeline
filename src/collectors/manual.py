"""
Manual company intake tool with automatic web research.

Single company:
    python -m src.collectors.manual "Acme Defense" --source "Conference"
    python -m src.collectors.manual "Acme Defense" --source "LinkedIn" \\
        --url https://acme.io --category "Autonomy and Unmanned Systems" --country "UK"

Batch mode (one company name per line):
    python -m src.collectors.manual --batch companies.txt --source "DSEI 2026"
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from duckduckgo_search import DDGS

try:
    from db.migrate import migrate as migrate_database
except ImportError:  # supports `python -m src.collectors.manual`
    from src.db.migrate import migrate as migrate_database

from .base import DB_PATH, fetch_page_text, store_lead
from .vocabulary import get_search_queries, map_category_to_sector

load_dotenv()

_CLIENT = anthropic.Anthropic()

_SEARCH_QUERIES   = 5    # top N queries to run
_URLS_TO_FETCH    = 3    # unique URLs to fetch for research
_RESULTS_PER_QUERY = 4   # DDG results per query
_BATCH_DELAY      = 2.0  # seconds between batch entries

# ---------------------------------------------------------------------------
# Claude research tool
# ---------------------------------------------------------------------------

_RESEARCH_TOOL = {
    "name": "extract_company_research",
    "description": (
        "Extract structured information about a defense or dual-use technology "
        "company from web search results and article text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "one_liner": {
                "type": "string",
                "description": (
                    "One sentence (max 20 words) describing what the company builds. "
                    "Start with a verb, e.g. 'Builds AI-powered drone swarm software for tactical ISR.'"
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "One paragraph (3–5 sentences) covering what they build, who their "
                    "customers are, and what makes them notable."
                ),
            },
            "founders": {
                "type": "string",
                "description": "Founder name(s) if mentioned, else empty string.",
            },
            "country": {
                "type": "string",
                "description": "Country of headquarters if determinable, else empty string.",
            },
            "funding_status": {
                "type": "string",
                "description": (
                    "Funding stage or amount if mentioned (e.g. 'Seed $2M', 'Series A', "
                    "'Bootstrapped', 'SBIR Phase II'), else empty string."
                ),
            },
            "website": {
                "type": "string",
                "description": "Company website URL if found, else empty string.",
            },
        },
        "required": [
            "one_liner", "description", "founders",
            "country", "funding_status", "website",
        ],
    },
}


def _search_and_fetch(company_name: str, sector_hint: str | None) -> tuple[list[str], str]:
    """
    Run web searches and fetch page text for a company.

    Returns (urls_fetched, combined_text).
    """
    queries = get_search_queries(company_name, sector_hint=sector_hint)[:_SEARCH_QUERIES]

    seen_urls: set[str] = set()
    candidate_urls: list[str] = []

    with DDGS() as ddgs:
        for query in queries:
            if len(candidate_urls) >= _URLS_TO_FETCH * 3:
                break
            try:
                results = ddgs.text(query, max_results=_RESULTS_PER_QUERY)
                for r in results:
                    url = r.get("href", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        candidate_urls.append(url)
            except Exception as e:
                print(f"  [search error] {e}", file=sys.stderr)
            time.sleep(0.8)

    fetched_urls: list[str] = []
    page_texts: list[str] = []

    for url in candidate_urls:
        if len(fetched_urls) >= _URLS_TO_FETCH:
            break
        text = fetch_page_text(url, max_chars=8_000)
        if text:
            fetched_urls.append(url)
            page_texts.append(f"[Source: {url}]\n{text}")

    combined = "\n\n---\n\n".join(page_texts)
    return fetched_urls, combined


def _research_company(company_name: str, sector_hint: str | None) -> dict | None:
    """Search web, fetch pages, extract structured info via Claude."""
    print(f"  Searching web for '{company_name}'...")
    fetched_urls, combined_text = _search_and_fetch(company_name, sector_hint)

    if not combined_text:
        print("  [warn] No web content retrieved — skipping research.")
        return None

    print(f"  Fetched {len(fetched_urls)} page(s). Extracting with Claude...")

    try:
        response = _CLIENT.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=(
                "You are a defense-sector analyst extracting structured company "
                "intelligence from web search results. Be factual and concise. "
                "Only report what the sources actually say — do not invent details."
            ),
            tools=[_RESEARCH_TOOL],
            tool_choice={"type": "tool", "name": "extract_company_research"},
            messages=[{
                "role": "user",
                "content": (
                    f"Research this company: {company_name}\n\n"
                    f"Web content retrieved:\n\n{combined_text}"
                ),
            }],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "extract_company_research":
                return block.input
    except Exception as e:
        print(f"  [claude error] {e}", file=sys.stderr)

    return None


def _update_description(company_name: str, source: str, one_liner: str) -> None:
    """Write one_liner to raw_leads.initial_description if currently NULL."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE raw_leads
               SET initial_description = ?
             WHERE company_name = ?
               AND source = ?
               AND initial_description IS NULL
            """,
            (one_liner, company_name, source),
        )
        conn.commit()


def _print_research(name: str, info: dict) -> None:
    print()
    print(f"  ┌─ Research: {name}")
    if info.get("one_liner"):
        print(f"  │  What:     {info['one_liner']}")
    if info.get("founders"):
        print(f"  │  Founders: {info['founders']}")
    if info.get("country"):
        print(f"  │  Country:  {info['country']}")
    if info.get("funding_status"):
        print(f"  │  Funding:  {info['funding_status']}")
    if info.get("website"):
        print(f"  │  Website:  {info['website']}")
    print(f"  │")
    for line in (info.get("description") or "").split(". "):
        line = line.strip()
        if line:
            print(f"  │  {line}.")
    print(f"  └{'─'*50}")


# ---------------------------------------------------------------------------
# Core intake function
# ---------------------------------------------------------------------------

def intake(
    company_name: str,
    source: str,
    url: str | None = None,
    description: str | None = None,
    category: str | None = None,
    country: str | None = None,
) -> None:
    """Insert one company and run auto-research."""
    migrate_database(DB_PATH)
    print(f"\n→ {company_name}  [{source}]")

    inserted = store_lead(
        company_name=company_name,
        source=source,
        source_url=url,
        initial_description=description,
        category_hint=category,
        country=country,
    )

    if inserted:
        print(f"  ✓ Added to raw_leads.")
    else:
        print(f"  ~ Already exists in raw_leads (duplicate). Running research anyway.")

    sector_hint = map_category_to_sector(category)
    info = _research_company(company_name, sector_hint)

    if info:
        _print_research(company_name, info)
        if info.get("one_liner") and not description:
            _update_description(company_name, source, info["one_liner"])

    print(f"\nAdded {company_name} to raw_leads from source {source}. Initial research complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Manual company intake with automatic web research.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Mutually exclusive: single name vs batch file
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("company_name", nargs="?", help="Company name to add")
    mode.add_argument("--batch", metavar="FILE",
                      help="Text file with one company name per line")

    p.add_argument("--source",      required=True, help='Source label, e.g. "DSEI 2026"')
    p.add_argument("--url",         default=None,  help="Company or article URL")
    p.add_argument("--description", default=None,  help="One-line description (optional)")
    p.add_argument("--category",    default=None,  help="Category hint, e.g. 'Autonomy and Unmanned Systems'")
    p.add_argument("--country",     default=None,  help="Country of HQ")
    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.batch:
        batch_file = Path(args.batch)
        if not batch_file.exists():
            print(f"Error: file not found: {batch_file}", file=sys.stderr)
            sys.exit(1)
        names = [
            line.strip()
            for line in batch_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        print(f"Batch mode: {len(names)} companies from {batch_file}")
        for i, name in enumerate(names, 1):
            print(f"\n[{i}/{len(names)}]", end="")
            intake(
                company_name=name,
                source=args.source,
                url=args.url,
                description=args.description,
                category=args.category,
                country=args.country,
            )
            if i < len(names):
                time.sleep(_BATCH_DELAY)
    else:
        if not args.company_name:
            parser.error("Provide a company name or use --batch")
        intake(
            company_name=args.company_name,
            source=args.source,
            url=args.url,
            description=args.description,
            category=args.category,
            country=args.country,
        )


if __name__ == "__main__":
    main()
