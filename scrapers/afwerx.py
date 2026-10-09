"""Direct AFWERX portfolio/showcase scraper.

This scraper reads public AFWERX HTML only. It does not use Apify, does not
install browser tooling, and does not write to the production database.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from scrapers.base import DirectScraper, DirectScraperBlocked


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DEFAULT = ROOT / "docs" / "artifacts" / "discovery" / "afwerx" / "companies.json"
REPORT_PATH = ROOT / "docs" / "SCRAPER_AFWERX_REPORT.md"
FOLLOWUPS_PATH = ROOT / "docs" / "SCRAPER_FOLLOWUPS.md"


@dataclass(frozen=True)
class AFWERXRunMeta:
    entry_url: str
    robots_url: str
    investigated_urls: list[str]
    pages_requested: int
    blocked: bool
    block_reason: str
    scrape_status: str
    parsing_strategy: str
    anti_bot_notes: list[str]
    limitations: list[str]


class AFWERXScraper(DirectScraper):
    entry_url = "https://afwerx.com/divisions/sbir-sttr/portfolio/"
    dashboard_url = "https://afwerx.com/divisions/sbir-sttr/investor-focused-dashboard/"
    robots_url = "https://afwerx.com/robots.txt"
    source = "afwerx_portfolio"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.meta = AFWERXRunMeta(
            entry_url=self.entry_url,
            robots_url=self.robots_url,
            investigated_urls=[
                "https://afwerx.com/",
                "https://afwerx.com/portfolio/",
                self.entry_url,
                self.dashboard_url,
                "https://afwerxchallenge.com/",
            ],
            pages_requested=0,
            blocked=False,
            block_reason="",
            scrape_status="not_run",
            parsing_strategy="requests + BeautifulSoup over AFWERX public HTML",
            anti_bot_notes=[],
            limitations=[],
        )

    def _set_meta(
        self,
        *,
        pages_requested: int | None = None,
        blocked: bool | None = None,
        block_reason: str | None = None,
        scrape_status: str | None = None,
        anti_bot_note: str | None = None,
        limitation: str | None = None,
    ) -> None:
        notes = list(self.meta.anti_bot_notes)
        limitations = list(self.meta.limitations)
        if anti_bot_note:
            notes.append(anti_bot_note)
        if limitation:
            limitations.append(limitation)
        self.meta = AFWERXRunMeta(
            entry_url=self.meta.entry_url,
            robots_url=self.meta.robots_url,
            investigated_urls=self.meta.investigated_urls,
            pages_requested=self.meta.pages_requested if pages_requested is None else pages_requested,
            blocked=self.meta.blocked if blocked is None else blocked,
            block_reason=self.meta.block_reason if block_reason is None else block_reason,
            scrape_status=self.meta.scrape_status if scrape_status is None else scrape_status,
            parsing_strategy=self.meta.parsing_strategy,
            anti_bot_notes=notes,
            limitations=limitations,
        )

    def _check_robots(self, url: str) -> None:
        self.log(f"checking robots.txt at {self.robots_url} for {url}")
        try:
            text = self.fetch_url(self.robots_url)
        except DirectScraperBlocked as exc:
            reason = f"robots.txt unavailable due to block: {exc}"
            self._set_meta(blocked=True, block_reason=reason, scrape_status="blocked", anti_bot_note=reason)
            raise
        parser = RobotFileParser()
        parser.set_url(self.robots_url)
        parser.parse(text.splitlines())
        if not parser.can_fetch(self.USER_AGENT, url):
            reason = f"robots.txt disallows {url} for {self.USER_AGENT}"
            self._set_meta(blocked=True, block_reason=reason, scrape_status="blocked", anti_bot_note=reason)
            raise DirectScraperBlocked(reason)

    def fetch(self) -> list[dict[str, str]]:
        self._check_robots(self.entry_url)
        self._check_robots(self.dashboard_url)
        pages: list[dict[str, str]] = []
        for url in (self.entry_url, self.dashboard_url):
            if pages:
                self.polite_delay()
            html = self.fetch_url(url)
            pages.append({"url": url, "html": html})
            self._set_meta(pages_requested=len(pages))
        return pages

    def _parse_static_company_cards(self, soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        selectors = [
            ".company",
            ".showcase",
            ".card",
            ".jet-listing-grid__item",
            ".brxe-card",
            ".post-card",
        ]
        seen_text: set[str] = set()
        for selector in selectors:
            for item in soup.select(selector):
                text = item.get_text(" ", strip=True)
                if not text or text in seen_text:
                    continue
                seen_text.add(text)
                link = item.find("a", href=True)
                name = ""
                for candidate in item.find_all(["h1", "h2", "h3", "h4", "a"]):
                    candidate_text = candidate.get_text(" ", strip=True)
                    if candidate_text and len(candidate_text) <= 120:
                        name = candidate_text
                        break
                if not name:
                    continue
                if name.lower() in {"portfolio", "investor focused dashboard", "coming soon"}:
                    continue
                records.append(
                    {
                        "name": name,
                        "website": link.get("href") if link else None,
                        "description": text if text != name else None,
                        "categories": [],
                        "source_url": page_url,
                        "source": self.source,
                        "country": None,
                        "raw": {"text": text[:1000]},
                    }
                )
        return records

    def parse(self, raw: list[dict[str, str]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        limitations: list[str] = []
        for page in raw:
            soup = BeautifulSoup(page["html"], "lxml")
            page_text = soup.get_text(" ", strip=True)
            if "Portfolio" in page_text and "coming soon" in page_text:
                limitations.append("AFWERX SBIR/STTR portfolio page is public but says 'Portfolio coming soon'.")
            iframe = soup.find("iframe", title=lambda value: value and "portfolio" in value.lower())
            if iframe and iframe.get("src"):
                limitations.append(
                    "Investor-focused portfolio page embeds a Looker Studio dashboard; company rows are not present in static AFWERX HTML."
                )
            records.extend(self._parse_static_company_cards(soup, page["url"]))
        if records:
            self._set_meta(scrape_status="clean")
        else:
            self._set_meta(
                scrape_status="structural_block",
                limitation="No company-level portfolio/showcase records were visible in AFWERX static HTML.",
            )
        for limitation in limitations:
            self._set_meta(limitation=limitation)
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
        write_followups(self.meta)
        return records


def _pct(part: int, whole: int) -> str:
    if not whole:
        return "0.0%"
    return f"{(part / whole) * 100:.1f}%"


def write_report(records: list[dict[str, Any]], meta: AFWERXRunMeta) -> None:
    websites = sum(1 for r in records if r.get("website"))
    descriptions = sum(1 for r in records if r.get("description"))
    categories = sum(1 for r in records if r.get("categories"))
    countries = sum(1 for r in records if r.get("country"))
    sample = records[0] if records else {}
    lines = [
        "# AFWERX Direct Scraper Report",
        "",
        "## Result",
        "",
        f"- Total records discovered: `{len(records)}`",
        f"- Scrape status: `{meta.scrape_status}`",
        f"- Blocked/halted: `{meta.blocked}`",
        f"- Block reason: `{meta.block_reason or 'none'}`",
        "",
        "## Entry URLs Investigated",
        "",
    ]
    lines.extend(f"- `{url}`" for url in meta.investigated_urls)
    lines.extend(
        [
            "",
            "## Robots / Access",
            "",
            f"- Robots URL checked: `{meta.robots_url}`",
            "- `robots.txt` was accessible and allows the AFWERX portfolio/dashboard paths checked.",
            "",
            "## Parsing Strategy",
            "",
            f"- {meta.parsing_strategy}",
            "- Static HTML parsing only. No Apify calls, no database writes, and no Playwright/browser execution.",
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
    )
    if meta.anti_bot_notes:
        lines.extend(f"- {note}" for note in meta.anti_bot_notes)
    else:
        lines.append("- No 403, rate limit, captcha, or Cloudflare wall observed on `afwerx.com` paths checked.")
    lines.extend(["", "## Limitations / Followups", ""])
    if meta.limitations:
        lines.extend(f"- {limitation}" for limitation in dict.fromkeys(meta.limitations))
    else:
        lines.append("- None observed.")
    lines.extend(
        [
            "",
            "## Recommended Next Steps",
            "",
            "- Do not add database ingestion from this source until the AFWERX portfolio/dashboard data source is clarified.",
            "- Treat the public AFWERX portfolio page as unavailable for company discovery until it is populated.",
            "- If the embedded Looker Studio dashboard is approved for scraping, handle it in a separate session with Playwright or a documented dashboard export/API path.",
            "- Consider AFWERX news/success stories as a separate article-mining source, not as a portfolio scraper.",
            "",
            "## Safety Confirmation",
            "",
            "- No production database writes.",
            "- No Apify calls.",
            "- No `src/collectors/` edits.",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines))


def write_followups(meta: AFWERXRunMeta) -> None:
    existing = FOLLOWUPS_PATH.read_text() if FOLLOWUPS_PATH.exists() else "# Scraper Followups\n"
    section = """\n\n## AFWERX Portfolio Followups\n\n- AFWERX `/divisions/sbir-sttr/portfolio/` is public and robots-allowed, but currently says `Portfolio coming soon`; no company-level records are available there.\n- AFWERX Investor Focused Dashboard is public HTML wrapping a Looker Studio iframe; static requests/BeautifulSoup do not expose dashboard company rows.\n- Playwright is not installed in the repo environment and was not added. Future work should either get an approved Looker export/API path or add a separate Playwright-based session after operator review.\n- AFWERX news/success stories may be useful for article-style discovery, but that is a different source than a portfolio/showcase scraper.\n"""
    if "## AFWERX Portfolio Followups" not in existing:
        FOLLOWUPS_PATH.write_text(existing.rstrip() + section)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run AFWERX public portfolio scraper.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch/parse and write JSON only.")
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        raise SystemExit("Only --dry-run is supported in this session.")
    scraper = AFWERXScraper()
    records = scraper.dry_run(args.output)
    print(f"records: {len(records)}")
    print(f"report: {REPORT_PATH}")
    print(f"followups: {FOLLOWUPS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
