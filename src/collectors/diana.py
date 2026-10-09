"""
NATO DIANA 2026 collector.

Searches the web for news articles and press releases naming specific companies
selected for the NATO DIANA 2026 cohort, then stores them as raw leads.

The diana.nato.int website is 403-blocked, so we find names via third-party
coverage: announcements, press releases, and news articles.

Run:
    python -m src.collectors.diana
"""

import json
import sys
import time
from collections import defaultdict

import anthropic
from dotenv import load_dotenv
from duckduckgo_search import DDGS

try:
    from db.migrate import migrate as migrate_database
except ImportError:  # supports `python -m src.collectors.diana`
    from src.db.migrate import migrate as migrate_database

from .base import DB_PATH
from .base import fetch_page_text, store_lead

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE = "NATO DIANA 2026"

CHALLENGE_AREAS = [
    "Energy and Power",
    "Advanced Communications",
    "Contested Electromagnetic Spectrum",
    "Autonomy and Unmanned Systems",
    "Data and Decision Making",
    "Human Resilience and Biotechnology",
    "Critical Infrastructure and Logistics",
    "Maritime Operations",
    "Extreme Environments",
    "Resilient Space Operations",
]

# Confirmed articles that name individual DIANA 2026 companies — fetched first.
SEED_URLS = [
    "https://www.gov.uk/government/news/the-uk-accelerator-welcomes-eight-companies-into-the-2026-nato-diana-programme",
    "https://spaceq.ca/nato-diana-selects-22-canadian-companies-for-2026-cohort/",
    "https://navyleaders.com/news/nato-diana-reveals-largest-ever-cohort-for-2026-challenge-programme/",
    "https://janusallies.org/janus-welcomes-new-nato-diana-2026-programme-cohort/",
    "https://thedefensepost.com/2025/12/15/nato-defense-tech-challenge/",
    "https://www.edrmagazine.eu/nato-defence-innovation-accelerator-announces-largest-ever-cohort-of-150-innovators-to-work-on-ten-defence-and-security-challenges-in-2026",
]

DDG_QUERIES = [
    ("NATO DIANA 2026 cohort companies", "General"),
    ("NATO DIANA 2026 selected companies list", "General"),
    ("DIANA accelerator 2026 startups selected", "General"),
    ("NATO DIANA 2026 cohort UK companies", "General"),
    ("NATO DIANA 2026 cohort Canada companies", "General"),
    ("NATO DIANA 2026 cohort US companies", "General"),
    ("NATO DIANA 2026 cohort Germany companies", "General"),
    ("NATO DIANA 2026 accelerator programme companies", "General"),
]

_RESULTS_PER_QUERY = 5
_INTER_REQUEST_SLEEP = 1.5  # seconds — avoids hammering DDG

# ---------------------------------------------------------------------------
# Claude extraction tool
# ---------------------------------------------------------------------------

_EXTRACT_TOOL = {
    "name": "extract_diana_companies",
    "description": (
        "Extract the names of companies that are explicitly identified as "
        "NATO DIANA 2026 cohort members or applicants in the provided article text. "
        "Only return companies that are clearly named as DIANA participants — "
        "do NOT include generic mentions of companies that are not specifically "
        "linked to the DIANA 2026 programme."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "companies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Official company name as written in the article.",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "One-sentence description of what the company does, "
                                "taken directly from the article context. "
                                "Empty string if not mentioned."
                            ),
                        },
                        "country": {
                            "type": "string",
                            "description": (
                                "Country of the company if mentioned in the article. "
                                "Empty string if unknown."
                            ),
                        },
                    },
                    "required": ["name", "description", "country"],
                },
                "description": "List of DIANA 2026 cohort companies found in the article.",
            }
        },
        "required": ["companies"],
    },
}

_CLIENT = anthropic.Anthropic()

_SYSTEM = (
    "You are a defense-sector research analyst extracting structured data from "
    "news articles and press releases about the NATO DIANA 2026 accelerator cohort. "
    "Be precise: only extract companies that are explicitly named as DIANA 2026 "
    "participants. Do not hallucinate company names."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _process_url(
    url: str,
    category: str,
    seen_urls: set[str],
    counters: dict,
) -> tuple[int, int]:
    """Fetch one URL, extract companies, store leads. Returns (inserted, duplicate)."""
    if url in seen_urls:
        return 0, 0
    seen_urls.add(url)

    print(f"  Fetching: {url}")
    page_text = fetch_page_text(url)
    if not page_text:
        print("  [skip] fetch failed or empty")
        return 0, 0

    companies = _extract_companies(page_text, url)
    if not companies:
        print("  [skip] no DIANA companies found in article")
        return 0, 0

    inserted = duplicate = 0
    for company in companies:
        name = company.get("name", "").strip()
        if not name:
            continue
        description = company.get("description", "").strip() or None
        country = company.get("country", "").strip() or None

        if store_lead(
            company_name=name,
            source=SOURCE,
            source_url=url,
            initial_description=description,
            category_hint=category,
            country=country,
        ):
            counters[category][0] += 1
            inserted += 1
            print(f"  + {name} ({country or 'country unknown'}) [{category}]")
        else:
            counters[category][1] += 1
            duplicate += 1
            print(f"  ~ {name} (duplicate)")

    return inserted, duplicate



def _extract_companies(page_text: str, article_url: str) -> list[dict]:
    """Ask Claude to extract DIANA 2026 company names from article text."""
    try:
        response = _CLIENT.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_SYSTEM,
            tools=[_EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "extract_diana_companies"},
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
            if block.type == "tool_use" and block.name == "extract_diana_companies":
                return block.input.get("companies", [])
    except Exception as e:
        print(f"  [claude error] {e}", file=sys.stderr)
    return []


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------


def run() -> None:
    migrate_database(DB_PATH)

    seen_urls: set[str] = set()
    counters: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    total_inserted = total_duplicate = 0

    # ------------------------------------------------------------------
    # Phase 1: seed URLs (confirmed to contain company names)
    # ------------------------------------------------------------------
    print(f"Phase 1: processing {len(SEED_URLS)} seed URLs")
    for i, url in enumerate(SEED_URLS, 1):
        print(f"\n[seed {i}/{len(SEED_URLS)}]")
        ins, dup = _process_url(url, "General", seen_urls, counters)
        total_inserted += ins
        total_duplicate += dup
        time.sleep(_INTER_REQUEST_SLEEP)

    # ------------------------------------------------------------------
    # Phase 2: DDG supplemental queries
    # ------------------------------------------------------------------
    print(f"\nPhase 2: running {len(DDG_QUERIES)} DDG queries")
    with DDGS() as ddgs:
        for i, (query, category) in enumerate(DDG_QUERIES, 1):
            print(f"\n[ddg {i}/{len(DDG_QUERIES)}] {query}")
            try:
                results = list(ddgs.text(query, max_results=_RESULTS_PER_QUERY))
            except Exception as e:
                print(f"  [search error] {e}", file=sys.stderr)
                time.sleep(_INTER_REQUEST_SLEEP * 2)
                continue

            for result in results:
                url = result.get("href", "")
                if not url:
                    continue
                ins, dup = _process_url(url, category, seen_urls, counters)
                total_inserted += ins
                total_duplicate += dup
                time.sleep(_INTER_REQUEST_SLEEP)

            time.sleep(_INTER_REQUEST_SLEEP)

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"NATO DIANA 2026 collector complete")
    print(f"  {total_inserted} new companies inserted")
    print(f"  {total_duplicate} duplicates skipped")
    print(f"  {len(seen_urls)} unique URLs fetched")
    print()

    if total_inserted > 0:
        print("Breakdown by challenge area:")
        for area in CHALLENGE_AREAS + ["General"]:
            ins, dup = counters.get(area, [0, 0])
            if ins + dup > 0:
                print(f"  {area:45s}  {ins:3d} new  {dup:3d} dup")
    print("=" * 60)


if __name__ == "__main__":
    run()
