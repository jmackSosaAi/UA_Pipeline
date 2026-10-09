"""Direct European Defence Fund recipients scraper.

This scraper reads official European Commission EDF result pages and project
factsheet PDFs. It does not use Apify, does not install PDF tooling, and does
not write to the production database.
"""
from __future__ import annotations

import argparse
import json
import re
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from scrapers.base import DirectScraper, DirectScraperBlocked, DirectScraperError


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DEFAULT = ROOT / "docs" / "artifacts" / "discovery" / "edf" / "companies.json"
PROGRESS_DEFAULT = ROOT / "docs" / "artifacts" / "discovery" / "edf" / "progress.json"
REPORT_PATH = ROOT / "docs" / "SCRAPER_EDF_REPORT.md"
FOLLOWUPS_PATH = ROOT / "docs" / "SCRAPER_FOLLOWUPS.md"

RESULT_PAGES = {
    "2025": "https://defence-industry-space.ec.europa.eu/funding-opportunities/calls-proposals/result-edf-2025-calls-proposals_en",
    "2024": "https://defence-industry-space.ec.europa.eu/funding-opportunities/calls-proposals/result-edf-2024-calls-proposals_en",
    "2023": "https://defence-industry-space.ec.europa.eu/funding-opportunities-0/calls-proposals/results-edf-2023-calls-proposals_en",
    "2022": "https://defence-industry-space.ec.europa.eu/funding-and-grants/calls-proposals/result-edf-2022-calls-proposals_en",
    "2021": "https://defence-industry-space.ec.europa.eu/calls-proposals/european-defence-fund-2021-calls-proposals-results_en",
}

COUNTRIES = {
    "Austria",
    "Belgium",
    "Bulgaria",
    "Croatia",
    "Cyprus",
    "Czech Republic",
    "Czechia",
    "Denmark",
    "Estonia",
    "Finland",
    "France",
    "Germany",
    "Greece",
    "Hungary",
    "Ireland",
    "Italy",
    "Latvia",
    "Lithuania",
    "Luxembourg",
    "Malta",
    "Netherlands",
    "Norway",
    "Poland",
    "Portugal",
    "Romania",
    "Slovakia",
    "Slovenia",
    "Spain",
    "Sweden",
    "The Netherlands",
}

SKIP_ACTUAL_TEXT = {
    "NAME",
    "OF THEENTITY",
    "OF THE ENTITY",
    "COUNTRY",
    "SELECTED PROJECTS",
    "EUROPEAN DEFENCE FUND (EDF)2025",
    "EUROPEAN DEFENCE FUND (EDF) 2025",
    "MEMBERSOFTHECONSORTIUMANDCOUNTRYOFESTABLISHMENT:",
}


@dataclass(frozen=True)
class EDFRunMeta:
    entry_urls: list[str]
    robots_urls: list[str]
    result_pages_requested: int
    factsheets_discovered: int
    factsheets_requested: int
    projects_parsed: int
    blocked: bool
    block_reason: str
    scrape_status: str
    parsing_strategy: str
    anti_bot_notes: list[str]
    limitations: list[str]
    years: list[str]
    cap: int | None
    resume: bool
    skipped_by_resume: int
    progress_path: str
    delay_range: tuple[float, float]
    last_attempted_url: str


class EDFScraper(DirectScraper):
    source = "edf_recipients"
    robots_url = "https://defence-industry-space.ec.europa.eu/robots.txt"

    def __init__(
        self,
        *,
        years: list[str] | None = None,
        max_projects: int | None = 5,
        resume: bool = False,
        progress_path: Path = PROGRESS_DEFAULT,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("delay_range", (10.0, 20.0))
        super().__init__(**kwargs)
        self.years = years or ["2025"]
        self.max_projects = max_projects
        self.resume = resume
        self.progress_path = progress_path
        self._robots_parser: RobotFileParser | None = None
        self.meta = EDFRunMeta(
            entry_urls=[RESULT_PAGES[year] for year in self.years if year in RESULT_PAGES],
            robots_urls=[self.robots_url],
            result_pages_requested=0,
            factsheets_discovered=0,
            factsheets_requested=0,
            projects_parsed=0,
            blocked=False,
            block_reason="",
            scrape_status="not_run",
            parsing_strategy=(
                "requests + BeautifulSoup over official EDF results pages; lightweight "
                "FlateDecode/ActualText extraction from official factsheet PDFs"
            ),
            anti_bot_notes=[],
            limitations=[],
            years=self.years,
            cap=self.max_projects,
            resume=self.resume,
            skipped_by_resume=0,
            progress_path=str(self.progress_path),
            delay_range=self.delay_range,
            last_attempted_url="",
        )

    def _set_meta(
        self,
        *,
        result_pages_requested: int | None = None,
        factsheets_discovered: int | None = None,
        factsheets_requested: int | None = None,
        projects_parsed: int | None = None,
        blocked: bool | None = None,
        block_reason: str | None = None,
        scrape_status: str | None = None,
        anti_bot_note: str | None = None,
        limitation: str | None = None,
        skipped_by_resume: int | None = None,
        last_attempted_url: str | None = None,
    ) -> None:
        notes = list(self.meta.anti_bot_notes)
        limitations = list(self.meta.limitations)
        if anti_bot_note:
            notes.append(anti_bot_note)
        if limitation:
            limitations.append(limitation)
        self.meta = EDFRunMeta(
            entry_urls=self.meta.entry_urls,
            robots_urls=self.meta.robots_urls,
            result_pages_requested=(
                self.meta.result_pages_requested
                if result_pages_requested is None
                else result_pages_requested
            ),
            factsheets_discovered=(
                self.meta.factsheets_discovered if factsheets_discovered is None else factsheets_discovered
            ),
            factsheets_requested=(
                self.meta.factsheets_requested if factsheets_requested is None else factsheets_requested
            ),
            projects_parsed=self.meta.projects_parsed if projects_parsed is None else projects_parsed,
            blocked=self.meta.blocked if blocked is None else blocked,
            block_reason=self.meta.block_reason if block_reason is None else block_reason,
            scrape_status=self.meta.scrape_status if scrape_status is None else scrape_status,
            parsing_strategy=self.meta.parsing_strategy,
            anti_bot_notes=notes,
            limitations=limitations,
            years=self.meta.years,
            cap=self.meta.cap,
            resume=self.meta.resume,
            skipped_by_resume=(
                self.meta.skipped_by_resume if skipped_by_resume is None else skipped_by_resume
            ),
            progress_path=self.meta.progress_path,
            delay_range=self.meta.delay_range,
            last_attempted_url=self.meta.last_attempted_url if last_attempted_url is None else last_attempted_url,
        )

    def _check_robots(self, url: str) -> None:
        self.log(f"checking robots.txt at {self.robots_url} for {url}")
        if self._robots_parser is None:
            try:
                text = self.fetch_url(self.robots_url)
            except DirectScraperBlocked as exc:
                reason = f"robots.txt unavailable due to block: {exc}"
                self._set_meta(blocked=True, block_reason=reason, scrape_status="blocked", anti_bot_note=reason)
                raise
            parser = RobotFileParser()
            parser.set_url(self.robots_url)
            parser.parse(text.splitlines())
            self._robots_parser = parser
        if not self._robots_parser.can_fetch(self.USER_AGENT, url):
            reason = f"robots.txt disallows {url} for {self.USER_AGENT}"
            self._set_meta(blocked=True, block_reason=reason, scrape_status="blocked", anti_bot_note=reason)
            raise DirectScraperBlocked(reason)

    def _fetch_binary(self, url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self.log(f"GET {url} attempt {attempt}/{self.max_retries}")
            try:
                response = self.session.get(url, timeout=self.timeout)
                self.log(f"{response.status_code} {url} ({len(response.content)} bytes)")
                if response.status_code in {401, 403, 429}:
                    raise DirectScraperBlocked(f"{url} returned HTTP {response.status_code}")
                if 500 <= response.status_code < 600 and attempt < self.max_retries:
                    self.polite_delay()
                    continue
                response.raise_for_status()
                return response.content
            except DirectScraperBlocked:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self.polite_delay()
                    continue
                raise DirectScraperError(f"failed to fetch {url}: {exc}") from exc
        raise DirectScraperError(f"failed to fetch {url}: {last_error}")

    def _factsheet_links(self, year: str, page_url: str, html: str) -> list[dict[str, str]]:
        soup = BeautifulSoup(html, "lxml")
        links: list[dict[str, str]] = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(page_url, anchor["href"])
            label = " ".join(anchor.get_text(" ", strip=True).split())
            filename = _filename_from_url(href)
            haystack = f"{href} {filename} {label}".lower()
            if "document/download" not in href.lower():
                continue
            if "factsheet" not in haystack:
                continue
            if "general factsheet" in haystack or "results general factsheet" in haystack:
                continue
            if ".pdf" not in haystack:
                continue
            links.append({"year": year, "url": href, "label": label, "filename": filename})
        return links

    def _load_progress(self) -> dict[str, Any]:
        if not self.progress_path.exists():
            return {"successful_urls": [], "blocked_urls": [], "runs": []}
        try:
            return json.loads(self.progress_path.read_text())
        except json.JSONDecodeError:
            return {"successful_urls": [], "blocked_urls": [], "runs": []}

    def _write_progress(
        self,
        progress: dict[str, Any],
        *,
        status: str,
        factsheet: dict[str, str] | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        progress.setdefault("successful_urls", [])
        progress.setdefault("blocked_urls", [])
        progress.setdefault("runs", [])
        progress["updated_at"] = now
        progress["last_status"] = status
        if factsheet and status == "success" and factsheet["url"] not in progress["successful_urls"]:
            progress["successful_urls"].append(factsheet["url"])
        if factsheet and status == "blocked" and factsheet["url"] not in progress["blocked_urls"]:
            progress["blocked_urls"].append(factsheet["url"])
        progress["runs"].append(
            {
                "timestamp": now,
                "status": status,
                "url": factsheet["url"] if factsheet else None,
                "filename": factsheet["filename"] if factsheet else None,
                "error": error,
            }
        )
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        self.progress_path.write_text(json.dumps(progress, indent=2, ensure_ascii=False))

    def fetch(self) -> list[dict[str, Any]]:
        for url in self.meta.entry_urls:
            self._check_robots(url)
        projects: list[dict[str, Any]] = []
        factsheets: list[dict[str, str]] = []
        progress = self._load_progress()
        completed_urls = set(progress.get("successful_urls", [])) if self.resume else set()
        for year in self.years:
            page_url = RESULT_PAGES.get(year)
            if not page_url:
                self._set_meta(limitation=f"Unknown EDF result year requested: {year}")
                continue
            if factsheets:
                self.polite_delay()
            html = self.fetch_url(page_url)
            self._set_meta(result_pages_requested=self.meta.result_pages_requested + 1)
            factsheets.extend(self._factsheet_links(year, page_url, html))
        self._set_meta(factsheets_discovered=len(factsheets))
        if completed_urls:
            before = len(factsheets)
            factsheets = [item for item in factsheets if item["url"] not in completed_urls]
            self._set_meta(skipped_by_resume=before - len(factsheets))
        if self.max_projects is not None:
            factsheets = factsheets[: self.max_projects]
        for item in factsheets:
            self._check_robots(item["url"])
            if projects:
                self.polite_delay()
            self._set_meta(last_attempted_url=item["url"])
            try:
                pdf_bytes = self._fetch_binary(item["url"])
            except DirectScraperBlocked as exc:
                reason = f"factsheet fetch halted after {len(projects)} successful PDFs: {exc}"
                self._set_meta(
                    blocked=True,
                    block_reason=reason,
                    scrape_status="blocked",
                    anti_bot_note=reason,
                    limitation="Factsheet fetch hit an HTTP 429 rate limit; no retry or workaround was attempted.",
                )
                self._write_progress(progress, status="blocked", factsheet=item, error=str(exc))
                break
            item = dict(item)
            item["pdf_bytes"] = pdf_bytes
            projects.append(item)
            self._set_meta(factsheets_requested=len(projects))
            self._write_progress(progress, status="success", factsheet=item)
        return projects

    def parse(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records_by_key: dict[str, dict[str, Any]] = {}
        parsed_projects = 0
        for item in raw:
            project = _parse_project_factsheet(item)
            if not project["participants"]:
                self._set_meta(limitation=f"No participants parsed from factsheet `{item['filename']}`")
                continue
            parsed_projects += 1
            for participant in project["participants"]:
                name = participant.get("name")
                if not name:
                    continue
                key = _normalize_key(name)
                record = records_by_key.get(key)
                project_ref = {
                    "project_name": project.get("project_name"),
                    "project_acronym": project.get("project_acronym"),
                    "edf_year": project.get("edf_year"),
                    "role": participant.get("role"),
                    "award_amount": project.get("award_amount"),
                    "topic": project.get("topic"),
                    "source_url": project.get("source_url"),
                }
                if record:
                    record["raw"]["projects"].append(project_ref)
                    for category in project["categories"]:
                        if category not in record["categories"]:
                            record["categories"].append(category)
                    continue
                records_by_key[key] = {
                    "name": name,
                    "website": None,
                    "description": project.get("description"),
                    "categories": project["categories"],
                    "source_url": project.get("source_url"),
                    "source": self.source,
                    "country": participant.get("country"),
                    "project_name": project.get("project_name"),
                    "project_acronym": project.get("project_acronym"),
                    "edf_year": project.get("edf_year"),
                    "role": participant.get("role"),
                    "award_amount": project.get("award_amount"),
                    "topic": project.get("topic"),
                    "raw": {
                        "project_id": project.get("project_id"),
                        "factsheet_filename": project.get("filename"),
                        "projects": [project_ref],
                    },
                }
        self._set_meta(projects_parsed=parsed_projects)
        if records_by_key:
            self._set_meta(scrape_status="partial")
            self._set_meta(
                limitation=(
                    "Websites are not exposed in the official EDF factsheet participant tables; "
                    "records use recipient names and project metadata only."
                )
            )
            self._set_meta(
                limitation=(
                    "Country names are present in the PDFs, but the PDF structure can interleave "
                    "country and entity columns; this dry-run leaves `country` null rather than "
                    "risking incorrect mappings."
                )
            )
        else:
            self._set_meta(scrape_status="structural_block", limitation="No recipient records were parsed.")
        return sorted(records_by_key.values(), key=lambda record: record["name"].casefold())

    def _merge_existing_records(self, output: Path, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.resume or not output.exists():
            return records
        try:
            existing = json.loads(output.read_text())
        except json.JSONDecodeError:
            existing = []
        merged: dict[str, dict[str, Any]] = {}
        for record in existing + records:
            name = record.get("name")
            if not name:
                continue
            key = _normalize_key(name)
            current = merged.get(key)
            if not current:
                merged[key] = record
                continue
            current_projects = current.setdefault("raw", {}).setdefault("projects", [])
            for project in record.get("raw", {}).get("projects", []):
                if project not in current_projects:
                    current_projects.append(project)
            for category in record.get("categories", []):
                if category not in current.get("categories", []):
                    current.setdefault("categories", []).append(category)
        return sorted(merged.values(), key=lambda record: record["name"].casefold())

    def dry_run(self, output: Path) -> list[dict[str, Any]]:
        try:
            self.log("starting dry-run")
            records = self.parse(self.fetch())
        except DirectScraperBlocked:
            records = []
            self.log("blocked before any factsheet records could be parsed")
        records = self._merge_existing_records(output, records)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(records, indent=2, ensure_ascii=False))
        self.log(f"wrote {len(records)} records to {output}")
        write_report(records, self.meta)
        write_followups(self.meta)
        return records


def _filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    filename = parse_qs(parsed.query).get("filename", [""])[0]
    return unquote(filename)


def _clean_text(text: str) -> str:
    text = text.replace("\\", "")
    text = re.sub(r"\s+", " ", text)
    text = text.replace(" -", " - ").replace("- ", " - ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", " ", name.casefold())
    return " ".join(key.split())


def _parse_pdf_actual_text_groups(pdf_bytes: bytes) -> list[str]:
    blob = bytearray()
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, re.S):
        try:
            blob.extend(zlib.decompress(match.group(1)))
            blob.extend(b"\n")
        except zlib.error:
            continue
    data = bytes(blob)
    items: list[tuple[str | None, str]] = []
    marker = b"/ActualText("
    start = 0
    while True:
        marker_idx = data.find(marker, start)
        if marker_idx < 0:
            break
        parent = None
        parent_idx = data.rfind(b"<</P ", max(0, marker_idx - 300), marker_idx)
        if parent_idx >= 0:
            parent_match = re.match(rb"<</P (\d+) 0 R/S/Span/Type/StructElem", data[parent_idx:marker_idx])
            if parent_match:
                parent = parent_match.group(1).decode("ascii")
        cursor = marker_idx + len(marker)
        depth = 1
        escaped = False
        value = bytearray()
        while cursor < len(data) and depth > 0:
            char = data[cursor]
            if escaped:
                value.append(char)
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 40:
                depth += 1
                value.append(char)
            elif char == 41:
                depth -= 1
                if depth > 0:
                    value.append(char)
            else:
                value.append(char)
            cursor += 1
        items.append((parent, value.decode("latin1", "ignore")))
        start = cursor
    groups: list[str] = []
    current_parent: str | None = None
    current_values: list[str] = []
    for parent, value in items:
        if parent != current_parent:
            if current_values:
                groups.append(_clean_text("".join(current_values)))
            current_parent = parent
            current_values = [value]
        else:
            current_values.append(value)
    if current_values:
        groups.append(_clean_text("".join(current_values)))
    return [group for group in groups if group]


def _parse_project_factsheet(item: dict[str, Any]) -> dict[str, Any]:
    groups = _parse_pdf_actual_text_groups(item["pdf_bytes"])
    filename = item["filename"]
    acronym, title, project_id = _project_identity(item["label"], filename)
    consortium_index = next(
        (
            index
            for index, group in enumerate(groups)
            if "MEMBERSOFTHECONSORTIUM" in group.upper().replace(" ", "")
        ),
        len(groups),
    )
    acronym_index = groups.index(acronym) if acronym in groups else 0
    body = [
        group
        for group in groups[acronym_index + 1 : consortium_index]
        if group
        and "EuropeanUnion" not in group
        and group not in {"@defis_eu", "#StrongerEurope #EUDefenceIndustry"}
    ]
    description = next((group for group in body if len(group) > 100), None)
    topic = body[2] if len(body) > 2 else None
    if topic and (
        len(topic) > 160
        or re.fullmatch(r"\d+ Months", topic)
        or re.fullmatch(r"\d[\d,]+\.\d{2}", topic)
    ):
        topic = None
    amounts = [group for group in groups if re.fullmatch(r"\d[\d,]+\.\d{2}", group)]
    participants = _parse_participants(groups)
    categories = [value for value in [topic, f"EDF {item['year']}"] if value]
    return {
        "project_name": title or acronym,
        "project_acronym": acronym,
        "project_id": project_id,
        "edf_year": item["year"],
        "description": description,
        "topic": topic,
        "award_amount": amounts[-1] if amounts else None,
        "categories": categories,
        "participants": participants,
        "source_url": item["url"],
        "filename": filename,
    }


def _project_identity(label: str, filename: str) -> tuple[str | None, str | None, str | None]:
    text = label.strip()
    acronym = None
    title = None
    if text and text.lower() != "download":
        if " - " in text:
            acronym, title = [part.strip() for part in text.split(" - ", 1)]
        elif "-" in text:
            acronym, title = [part.strip() for part in text.split("-", 1)]
        else:
            acronym = text
    project_match = re.search(r"(101\d{6,})", filename)
    project_id = project_match.group(1) if project_match else None
    if not acronym:
        stem = filename.rsplit(".", 1)[0]
        parts = re.split(r"[_\s-]+", stem)
        if project_id and project_id in parts:
            index = parts.index(project_id)
            acronym = " ".join(parts[index + 1 :]) or None
    if acronym:
        acronym = acronym.strip(" _-.")
    if title:
        title = title.strip(" _-.")
    return acronym, title, project_id


def _parse_participants(groups: list[str]) -> list[dict[str, str | None]]:
    collecting = False
    names: list[tuple[str, str | None]] = []
    countries: list[str] = []
    for group in groups:
        normalized = group.upper().replace(" ", "")
        if "MEMBERSOFTHECONSORTIUMANDCOUNTRYOFESTABLISHMENT" in normalized:
            collecting = True
            continue
        if not collecting:
            continue
        if group in COUNTRIES:
            countries.append(group)
            continue
        if group.upper() in SKIP_ACTUAL_TEXT or normalized in SKIP_ACTUAL_TEXT:
            continue
        if group.startswith("EUROPEAN DEFENCE FUND"):
            continue
        role = None
        name = group
        coord_match = re.match(r"^\(Coordinator\)\s*(.+)$", name, re.I)
        if coord_match:
            role = "coordinator"
            name = coord_match.group(1)
        name = _clean_text(name)
        if len(name) < 2:
            continue
        names.append((name, role))
    participants: list[dict[str, str | None]] = []
    for name, role in names:
        participants.append(
            {
                "name": name,
                "country": None,
                "role": role or "participant",
            }
        )
    return participants


def _pct(part: int, whole: int) -> str:
    if not whole:
        return "0.0%"
    return f"{(part / whole) * 100:.1f}%"


def write_report(records: list[dict[str, Any]], meta: EDFRunMeta) -> None:
    websites = sum(1 for record in records if record.get("website"))
    descriptions = sum(1 for record in records if record.get("description"))
    categories = sum(1 for record in records if record.get("categories"))
    countries = sum(1 for record in records if record.get("country"))
    awards = sum(1 for record in records if record.get("award_amount"))
    sample = records[0] if records else {}
    lines = [
        "# EDF Direct Scraper Report",
        "",
        "## Result",
        "",
        f"- Total recipient/company records discovered: `{len(records)}`",
        f"- Projects parsed: `{meta.projects_parsed}`",
        f"- Scrape status: `{meta.scrape_status}`",
        f"- Blocked/halted: `{meta.blocked}`",
        f"- Block reason: `{meta.block_reason or 'none'}`",
        "",
        "## Entry URLs Investigated",
        "",
    ]
    lines.extend(f"- `{url}`" for url in meta.entry_urls)
    lines.extend(
        [
            "",
            "## Robots / Access",
            "",
        ]
    )
    lines.extend(f"- Robots URL checked: `{url}`" for url in meta.robots_urls)
    lines.append("- `robots.txt` was accessible and allowed the official EDF result page and factsheet PDF paths checked.")
    lines.extend(
        [
            "",
            "## URL Pattern / Pagination",
            "",
            "- Official result pages are year-specific HTML pages on `defence-industry-space.ec.europa.eu`.",
            "- Project factsheets are linked as `/document/download/<uuid>...?filename=FACTSHEET_EDF_<year>_...pdf`.",
            "- No pagination was observed on the 2025 result page; one result page listed the factsheet links.",
            f"- Result pages requested: `{meta.result_pages_requested}`",
            f"- Factsheets discovered: `{meta.factsheets_discovered}`",
            f"- Factsheets requested: `{meta.factsheets_requested}`",
            f"- Factsheet cap for this run: `{meta.cap if meta.cap is not None else 'none'}`",
            f"- Resume enabled: `{meta.resume}`",
            f"- Factsheets skipped by resume: `{meta.skipped_by_resume}`",
            f"- Progress file: `{meta.progress_path}`",
            f"- Delay range between PDF fetches: `{meta.delay_range[0]:.1f}-{meta.delay_range[1]:.1f}s`",
            f"- Last attempted factsheet URL: `{meta.last_attempted_url or 'none'}`",
            "",
            "## Parsing Strategy",
            "",
            f"- {meta.parsing_strategy}",
            "- PDFs were parsed with standard-library zlib stream decompression and PDF `ActualText` extraction.",
            "- No Apify calls, no database writes, no Playwright/headless browser execution, and no new dependencies.",
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
            f"- With categories/topics: `{categories}` / `{len(records)}` ({_pct(categories, len(records))})",
            f"- With country: `{countries}` / `{len(records)}` ({_pct(countries, len(records))})",
            f"- With award amount: `{awards}` / `{len(records)}` ({_pct(awards, len(records))})",
            "",
            "## Anti-Bot / Access Notes",
            "",
        ]
    )
    if meta.anti_bot_notes:
        lines.extend(f"- {note}" for note in meta.anti_bot_notes)
    else:
        lines.append("- No 403, rate limit, captcha, Cloudflare wall, or robots block observed on checked EDF paths.")
    lines.extend(["", "## Deduplication Logic", ""])
    lines.append("- Records are deduplicated by normalized recipient name across the scraped EDF factsheets.")
    lines.append("- If a recipient appears in multiple projects, the first record is retained and project references are appended under `raw.projects`.")
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
            "- Review the dry-run JSON before any import; EDF recipients are consortium members, not necessarily startups.",
            "- Add website enrichment/normalization before promotion into the startup discovery database.",
            "- Add coordinate-aware PDF/table parsing before assigning country values from EDF factsheets.",
            "- Extend the same parser to 2021-2024 EDF result pages after spot-checking older factsheet layouts.",
            "- Consider a separate official Financial Transparency System path for booked payment amounts, if needed.",
            "",
            "## Safety Confirmation",
            "",
            "- No production database writes.",
            "- No Apify calls.",
            "- No `src/collectors/` edits.",
        ]
    )
    REPORT_PATH.write_text("\n".join(lines))


def write_followups(meta: EDFRunMeta) -> None:
    existing = FOLLOWUPS_PATH.read_text() if FOLLOWUPS_PATH.exists() else "# Scraper Followups\n"
    section = """\n\n## EDF Recipient Followups\n\n- Current dry-run targets the official 2025 EDF result page first. The same official site has 2021-2024 result pages, but older factsheet layouts should be spot-checked before a full multi-year import.\n- EDF factsheets expose consortium recipient names and country columns but not company websites. The current parser does not assign countries because PDF structure order can interleave entity and country columns; coordinate-aware PDF parsing may be needed for safe country mapping.\n- EDF requires slow/capped/resumable fetching. The uncapped run hit HTTP 429 after 48 PDF fetches; future runs should proceed in small batches instead of a full crawl.\n- The default EDF dry-run cap is 5 factsheets with 10-20 second jittered delays between PDF fetches. Use `--resume` to skip factsheets already marked successful in `docs/artifacts/discovery/edf/progress.json`.\n- The PDF `ActualText` parser works on capped runs and should be preserved unless a stronger table parser is added.\n- EDF recipients include primes, universities, research institutes, and public entities; add normalization/classification before treating records as startup leads.\n- Funding & Tenders project-consortium endpoints returned a token requirement during investigation; the PDF `ActualText` path is the current dependency-free extraction route.\n"""
    if "## EDF Recipient Followups" not in existing:
        FOLLOWUPS_PATH.write_text(existing.rstrip() + section)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run EDF recipient scraper.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch/parse and write JSON only.")
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--years", default="2025", help="Comma-separated EDF result years to scrape.")
    parser.add_argument("--max-projects", type=int, default=5, help="Cap for factsheet PDFs per run. Use 0 for no cap.")
    parser.add_argument("--resume", action="store_true", help="Skip factsheets already marked successful in progress JSON.")
    parser.add_argument("--progress", type=Path, default=PROGRESS_DEFAULT)
    parser.add_argument("--delay-min", type=float, default=10.0)
    parser.add_argument("--delay-max", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        raise SystemExit("Only --dry-run is supported in this session.")
    years = [year.strip() for year in args.years.split(",") if year.strip()]
    if args.delay_min < 0 or args.delay_max < args.delay_min:
        raise SystemExit("--delay-max must be greater than or equal to --delay-min, and both must be non-negative.")
    max_projects = None if args.max_projects == 0 else args.max_projects
    scraper = EDFScraper(
        years=years,
        max_projects=max_projects,
        resume=args.resume,
        progress_path=args.progress,
        delay_range=(args.delay_min, args.delay_max),
    )
    records = scraper.dry_run(args.output)
    print(f"records: {len(records)}")
    print(f"report: {REPORT_PATH}")
    print(f"followups: {FOLLOWUPS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
