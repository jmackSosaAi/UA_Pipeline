"""Direct In-Q-Tel portfolio scraper.

This scraper reads the public IQT portfolio HTML only. It does not use Apify,
Playwright, or write to the production database.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from scrapers.base import DirectScraper, DirectScraperBlocked


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DEFAULT = ROOT / "docs" / "artifacts" / "discovery" / "iqt_portfolio" / "companies.json"
REPORT_PATH = ROOT / "docs" / "SCRAPER_IQT_REPORT.md"


@dataclass(frozen=True)
class IQTRunMeta:
    entry_url: str
    robots_url: str
    pages_requested: int
    total_pages_seen: int | None
    blocked: bool
    block_reason: str
    anti_bot_notes: list[str]
    pagination_scheme: str


class IQTScraper(DirectScraper):
    entry_url = "https://www.iqt.org/portfolio"
    robots_url = "https://www.iqt.org/robots.txt"
    source = "iqt_portfolio"
    pagination_scheme = "https://www.iqt.org/portfolio?c6fb1237_page={page}"

    def __init__(self, *, max_pages: int | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_pages = max_pages
        self.meta = IQTRunMeta(
            entry_url=self.entry_url,
            robots_url=self.robots_url,
            pages_requested=0,
            total_pages_seen=None,
            blocked=False,
            block_reason="",
            anti_bot_notes=[],
            pagination_scheme=self.pagination_scheme,
        )

    def _set_meta(
        self,
        *,
        pages_requested: int | None = None,
        total_pages_seen: int | None = None,
        blocked: bool | None = None,
        block_reason: str | None = None,
        anti_bot_note: str | None = None,
    ) -> None:
        notes = list(self.meta.anti_bot_notes)
        if anti_bot_note:
            notes.append(anti_bot_note)
        self.meta = IQTRunMeta(
            entry_url=self.meta.entry_url,
            robots_url=self.meta.robots_url,
            pages_requested=self.meta.pages_requested if pages_requested is None else pages_requested,
            total_pages_seen=self.meta.total_pages_seen if total_pages_seen is None else total_pages_seen,
            blocked=self.meta.blocked if blocked is None else blocked,
            block_reason=self.meta.block_reason if block_reason is None else block_reason,
            anti_bot_notes=notes,
            pagination_scheme=self.meta.pagination_scheme,
        )

    def _check_robots(self) -> None:
        self.log(f"checking robots.txt at {self.robots_url}")
        try:
            text = self.fetch_url(self.robots_url)
        except DirectScraperBlocked as exc:
            reason = f"robots.txt unavailable due to block: {exc}"
            self._set_meta(blocked=True, block_reason=reason, anti_bot_note=reason)
            raise
        parser = RobotFileParser()
        parser.set_url(self.robots_url)
        parser.parse(text.splitlines())
        if not parser.can_fetch(self.USER_AGENT, self.entry_url):
            reason = f"robots.txt disallows portfolio path for {self.USER_AGENT}"
            self._set_meta(blocked=True, block_reason=reason, anti_bot_note=reason)
            raise DirectScraperBlocked(reason)

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self.entry_url
        return self.pagination_scheme.format(page=page)

    def _page_count(self, soup: BeautifulSoup) -> tuple[int | None, int | None]:
        marker = soup.select_one(".w-page-count")
        if not marker:
            return None, None
        match = re.search(r"(\d+)\s*/\s*(\d+)", marker.get_text(" ", strip=True))
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    def fetch(self) -> list[dict[str, str]]:
        self._check_robots()
        pages: list[dict[str, str]] = []
        page = 1
        total_pages: int | None = None
        while True:
            if pages:
                self.polite_delay()
            url = self._page_url(page)
            html = self.fetch_url(url)
            pages.append({"url": url, "html": html})
            soup = BeautifulSoup(html, "lxml")
            current, observed_total = self._page_count(soup)
            if observed_total:
                total_pages = observed_total
                self._set_meta(total_pages_seen=observed_total)
            self._set_meta(pages_requested=len(pages))
            if not soup.select(".ci-categories.w-dyn-item"):
                if page == 1:
                    reason = "portfolio page contained no inline company rows; possible JS-only rendering"
                    self._set_meta(blocked=True, block_reason=reason, anti_bot_note=reason)
                    raise DirectScraperBlocked(reason)
                break
            if self.max_pages and page >= self.max_pages:
                break
            if total_pages and current and current >= total_pages:
                break
            if not soup.select_one("a.w-pagination-next"):
                break
            page += 1
        return pages

    def _parse_item(self, item: Any, page_url: str) -> dict[str, Any] | None:
        name_el = item.select_one(".cl-company")
        link_el = item.select_one("a.lb-company-list[href]")
        name = name_el.get_text(" ", strip=True) if name_el else ""
        if not name:
            return None
        website = link_el.get("href") if link_el else None
        categories = [
            el.get_text(" ", strip=True)
            for el in item.select(".text-category")
            if el.get_text(" ", strip=True)
        ]
        location_el = item.select_one(".cl-location")
        status_el = item.select_one(".cl-status")
        location = location_el.get_text(" ", strip=True) if location_el else None
        status = status_el.get_text(" ", strip=True) if status_el else None
        iqt_portfolio_page = page_url
        return {
            "name": name,
            "website": website,
            "description": None,
            "categories": categories,
            "country": None,
            "iqt_portfolio_page": iqt_portfolio_page,
            "source_url": iqt_portfolio_page,
            "source": self.source,
            "raw": {
                "location": location,
                "status": status,
                "portfolio_page": page_url,
                "note": "IQT list exposes region/status/category; country and description are not present in listing HTML",
            },
        }

    def parse(self, raw: list[dict[str, str]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        for page in raw:
            soup = BeautifulSoup(page["html"], "lxml")
            for item in soup.select(".ci-categories.w-dyn-item"):
                record = self._parse_item(item, page["url"])
                if not record:
                    continue
                key = (record["name"].casefold(), record.get("website"))
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
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


def write_report(records: list[dict[str, Any]], meta: IQTRunMeta) -> None:
    websites = sum(1 for r in records if r.get("website"))
    descriptions = sum(1 for r in records if r.get("description"))
    categories = sum(1 for r in records if r.get("categories"))
    countries = sum(1 for r in records if r.get("country"))
    sample = records[0] if records else {}
    lines = [
        "# IQT Direct Scraper Report",
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
        f"- Pagination: `{meta.pagination_scheme}`",
        f"- Pages requested: `{meta.pages_requested}`",
        f"- Total pages observed: `{meta.total_pages_seen or 'unknown'}`",
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
        f"- With country: `{countries}` / `{len(records)}` ({_pct(countries, len(records))})",
        "",
        "## Anti-Bot / Access Notes",
        "",
    ]
    if meta.anti_bot_notes:
        lines.extend(f"- {note}" for note in meta.anti_bot_notes)
    else:
        lines.append("- No anti-bot blocks, rate limits, captchas, or JS-only rendering issues observed.")
    lines.extend(
        [
            "",
            "The portfolio rows are present in server-rendered Webflow HTML. No Playwright/headless browser path was used.",
            "",
            "## Estimated Full-Portfolio Cost",
            "",
            f"The observed scrape required `{meta.pages_requested}` HTML requests. At the default 1-2 second polite delay, full collection is roughly under one minute and low bandwidth; the generated JSON artifact is small enough for review/commit.",
            "",
            "## Recommended Next Steps",
            "",
            "- Review the JSON artifact before adding any database writes.",
            "- Decide whether IQT region labels should map to countries or stay as raw `location` metadata.",
            "- Add a production ingestion checkpoint that deduplicates on normalized name plus website.",
            "- Consider a separate detail-enrichment pass only for companies needing descriptions; the list page itself does not expose descriptions.",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run IQT public portfolio scraper.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch/parse and write JSON only.")
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--max-pages", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        raise SystemExit("Only --dry-run is supported in this session.")
    scraper = IQTScraper(max_pages=args.max_pages)
    records = scraper.dry_run(args.output)
    print(f"records: {len(records)}")
    print(f"report: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

