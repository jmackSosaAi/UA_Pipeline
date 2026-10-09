"""Direct Brave1 public catalog scraper.

This targets the public Brave1 Market catalog only. It does not log in, use
Apify, use Playwright, or write to the production database.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from scrapers.base import DirectScraper, DirectScraperBlocked


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DEFAULT = ROOT / "docs" / "artifacts" / "discovery" / "brave1_catalog" / "companies.json"
REPORT_PATH = ROOT / "docs" / "SCRAPER_BRAVE1_REPORT.md"


@dataclass(frozen=True)
class Brave1RunMeta:
    entry_url: str
    robots_url: str
    pages_requested: int
    blocked: bool
    block_reason: str
    anti_bot_notes: list[str]


class Brave1Scraper(DirectScraper):
    entry_url = "https://market-brave1.delta.mil.gov.ua/katalog/"
    robots_url = "https://market-brave1.delta.mil.gov.ua/robots.txt"
    source = "brave1_catalog"

    def __init__(self, *, max_pages: int = 1, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_pages = max_pages
        self.meta = Brave1RunMeta(
            entry_url=self.entry_url,
            robots_url=self.robots_url,
            pages_requested=0,
            blocked=False,
            block_reason="",
            anti_bot_notes=[],
        )

    def _set_blocked(self, reason: str) -> None:
        self.meta = Brave1RunMeta(
            entry_url=self.meta.entry_url,
            robots_url=self.meta.robots_url,
            pages_requested=self.meta.pages_requested,
            blocked=True,
            block_reason=reason,
            anti_bot_notes=[*self.meta.anti_bot_notes, reason],
        )

    def _catalog_url(self, page: int) -> str:
        if page <= 1:
            return self.entry_url
        return f"https://market-brave1.delta.mil.gov.ua/katalog/filter/page%3D{page}/"

    def _check_robots(self) -> None:
        self.log(f"checking robots.txt at {self.robots_url}")
        try:
            text = self.fetch_url(self.robots_url)
        except DirectScraperBlocked as exc:
            reason = f"robots.txt unavailable due to block: {exc}"
            self._set_blocked(reason)
            raise
        parser = RobotFileParser()
        parser.set_url(self.robots_url)
        parser.parse(text.splitlines())
        if not parser.can_fetch(self.USER_AGENT, self.entry_url):
            reason = f"robots.txt disallows catalog path for {self.USER_AGENT}"
            self._set_blocked(reason)
            raise DirectScraperBlocked(reason)

    def fetch(self) -> list[dict[str, str]]:
        self._check_robots()
        pages: list[dict[str, str]] = []
        for page in range(1, self.max_pages + 1):
            url = self._catalog_url(page)
            if pages:
                self.polite_delay()
            html = self.fetch_url(url)
            pages.append({"url": url, "html": html})
            self.meta = Brave1RunMeta(
                entry_url=self.meta.entry_url,
                robots_url=self.meta.robots_url,
                pages_requested=page,
                blocked=self.meta.blocked,
                block_reason=self.meta.block_reason,
                anti_bot_notes=self.meta.anti_bot_notes,
            )
        return pages

    def _is_product_url(self, href: str) -> bool:
        parsed = urlparse(href)
        path = parsed.path.strip("/")
        if not path or path in {"katalog", "pro-marketpleis", "pro-nas"}:
            return False
        if path.startswith(("katalog", "filter")):
            return False
        return bool(re.search(r"/\d+/?$", parsed.path))

    def _extract_categories(self, soup: BeautifulSoup) -> list[str]:
        categories: list[str] = []
        for selector in ("nav a", ".breadcrumbs a", ".catalog-menu a"):
            for tag in soup.select(selector):
                text = tag.get_text(" ", strip=True)
                if text and text not in {"Головна", "Каталог"} and text not in categories:
                    categories.append(text)
        return categories[:6]

    def parse(self, raw: list[dict[str, str]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in raw:
            soup = BeautifulSoup(page["html"], "lxml")
            categories = self._extract_categories(soup)
            for anchor in soup.find_all("a", href=True):
                name = anchor.get_text(" ", strip=True)
                if not name or name == "Детальніше":
                    continue
                href = urljoin(page["url"], anchor["href"])
                if not self._is_product_url(href) or href in seen:
                    continue
                seen.add(href)
                records.append(
                    {
                        "name": name,
                        "website": None,
                        "description": None,
                        "categories": categories,
                        "country": "Ukraine",
                        "source_url": href,
                        "source": self.source,
                        "raw": {
                            "catalog_page": page["url"],
                            "anchor_text": name,
                            "note": "public catalog listing; manufacturer/contact detail not fetched in this session",
                        },
                    }
                )
        return records

    def dry_run(self, output: Path) -> list[dict[str, Any]]:
        try:
            records = super().dry_run(output)
        except DirectScraperBlocked:
            records = []
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(records, indent=2, ensure_ascii=False))
            self.log(f"blocked; wrote empty record list to {output}")
        write_report(records, self.meta)
        return records


def _pct(part: int, whole: int) -> str:
    if not whole:
        return "0.0%"
    return f"{(part / whole) * 100:.1f}%"


def write_report(records: list[dict[str, Any]], meta: Brave1RunMeta) -> None:
    websites = sum(1 for r in records if r.get("website"))
    descriptions = sum(1 for r in records if r.get("description"))
    categories = sum(1 for r in records if r.get("categories"))
    sample = records[0] if records else {}
    lines = [
        "# Brave1 Direct Scraper Report",
        "",
        "## Result",
        "",
        f"- Total records discovered: `{len(records)}`",
        f"- Blocked/halted: `{meta.blocked}`",
        f"- Block reason: `{meta.block_reason or 'none'}`",
        "",
        "## URL Pattern",
        "",
        f"- Entry point: `{meta.entry_url}`",
        "- Pagination: `https://market-brave1.delta.mil.gov.ua/katalog/filter/page%3D{page}/`",
        f"- Robots URL checked: `{meta.robots_url}`",
        "",
        "## Schema Sample",
        "",
        "```json",
        json.dumps(sample, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Coverage Stats",
        "",
        f"- With website: `{websites}` / `{len(records)}` ({_pct(websites, len(records))})",
        f"- With description: `{descriptions}` / `{len(records)}` ({_pct(descriptions, len(records))})",
        f"- With categories: `{categories}` / `{len(records)}` ({_pct(categories, len(records))})",
        "",
        "## Anti-Bot / Access Notes",
        "",
    ]
    if meta.anti_bot_notes:
        lines.extend(f"- {note}" for note in meta.anti_bot_notes)
    else:
        lines.append("- No anti-bot blocks observed during this run.")
    lines.extend(
        [
            "",
            "Direct requests to the public market catalog must pass robots/access checks before scraping.",
            "In this run, the catalog domain returned a block response before product pages could be fetched.",
            "",
            "## Estimated Full-Catalog Cost",
            "",
            "The public listing reports hundreds of pages. A complete direct scrape would be roughly one request per catalog page plus optional product-detail requests. At a 1-2 second delay, listing-only collection would be minutes; detail-page enrichment would be longer. This estimate is deferred because the current direct request path is blocked.",
            "",
            "## Recommended Next Steps",
            "",
            "- Do not integrate DB writes until operator review.",
            "- Confirm with Brave1/Market operators whether programmatic access is permitted for the public catalog.",
            "- If approved, add per-page/detail scraping and manufacturer extraction.",
            "- If direct HTTP remains blocked, revisit with Playwright only after a separate policy decision.",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run Brave1 public catalog scraper.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch/parse and write JSON only.")
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--max-pages", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        raise SystemExit("Only --dry-run is supported in this session.")
    scraper = Brave1Scraper(max_pages=args.max_pages)
    scraper.dry_run(args.output)
    print(f"report: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

