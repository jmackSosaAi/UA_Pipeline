"""Base primitives for direct, non-Apify discovery scrapers."""
from __future__ import annotations

import abc
import json
import random
import time
from pathlib import Path
from typing import Any

import requests


class DirectScraperError(RuntimeError):
    """Base exception for direct scraper failures."""


class DirectScraperBlocked(DirectScraperError):
    """Raised when a source blocks direct scraping or robots cannot be checked."""


class DirectScraper(abc.ABC):
    """Small ABC for direct HTTP scrapers that write dry-run JSON artifacts only."""

    USER_AGENT = "UAPipeline/0.1 (+direct-discovery-research)"

    def __init__(
        self,
        *,
        delay_range: tuple[float, float] = (1.0, 2.0),
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.delay_range = delay_range
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
                "Accept-Language": "uk,en;q=0.8",
            }
        )

    def log(self, message: str) -> None:
        print(f"[{self.__class__.__name__}] {message}", flush=True)

    def polite_delay(self) -> None:
        delay = random.uniform(*self.delay_range)
        self.log(f"sleeping {delay:.1f}s")
        time.sleep(delay)

    def fetch_url(self, url: str) -> str:
        """Fetch one URL with polite retry/backoff on transient 5xx errors."""
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self.log(f"GET {url} attempt {attempt}/{self.max_retries}")
            try:
                response = self.session.get(url, timeout=self.timeout)
                self.log(f"{response.status_code} {url} ({len(response.text)} chars)")
                if response.status_code in {401, 403, 429}:
                    raise DirectScraperBlocked(f"{url} returned HTTP {response.status_code}")
                if 500 <= response.status_code < 600 and attempt < self.max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                response.raise_for_status()
                return response.text
            except DirectScraperBlocked:
                raise
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise DirectScraperError(f"failed to fetch {url}: {exc}") from exc
        raise DirectScraperError(f"failed to fetch {url}: {last_error}")

    @abc.abstractmethod
    def fetch(self) -> Any:
        """Return raw HTML/JSON for the source."""

    @abc.abstractmethod
    def parse(self, raw: Any) -> list[dict[str, Any]]:
        """Return normalized discovery records."""

    def dry_run(self, output: Path) -> list[dict[str, Any]]:
        """Fetch, parse, and write JSON records without database writes."""
        self.log("starting dry-run")
        records = self.parse(self.fetch())
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(records, indent=2, ensure_ascii=False))
        self.log(f"wrote {len(records)} records to {output}")
        return records

