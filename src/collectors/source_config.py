"""
Loaders for `config/sources/*.yaml`.

These keep collector-side data (article URLs, RSS feeds, excluded primes)
out of the investment-thesis config so each concern lives in one place.
"""

from functools import lru_cache
from pathlib import Path

import yaml

_SOURCES_DIR = Path(__file__).parent.parent.parent / "config" / "sources"

_ARTICLES_PATH = _SOURCES_DIR / "article_seed_urls.yaml"
_FEEDS_PATH = _SOURCES_DIR / "rss_feeds.yaml"
_EXCLUDED_PATH = _SOURCES_DIR / "excluded_companies.yaml"
_SBIR_PATH = _SOURCES_DIR / "sbir_config.yaml"


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def load_article_seed_urls() -> list[dict]:
    """Return the curated article seed list as a list of {url, category, description}."""
    return list(_read_yaml(_ARTICLES_PATH).get("articles", []))


@lru_cache(maxsize=2)
def load_rss_feeds(active_only: bool = True) -> dict[str, str]:
    """Return {feed_name: feed_url}, filtered to active feeds by default."""
    feeds = _read_yaml(_FEEDS_PATH).get("feeds", [])
    return {
        f["name"]: f["url"]
        for f in feeds
        if not active_only or f.get("active", True)
    }


@lru_cache(maxsize=1)
def load_excluded_companies() -> set[str]:
    """Return a lowercased set of company names to exclude from raw_leads."""
    excluded = _read_yaml(_EXCLUDED_PATH).get("excluded", [])
    return {e["name"].strip().lower() for e in excluded if e.get("name")}


@lru_cache(maxsize=1)
def load_noise_description_patterns() -> list[str]:
    """Return lowercased substring patterns that disqualify a description as
    'real defense company'. Read from excluded_companies.yaml so all promotion
    blocklists live in one file."""
    raw = _read_yaml(_EXCLUDED_PATH).get("noise_description_patterns", []) or []
    return [p.strip().lower() for p in raw if p and p.strip()]


@lru_cache(maxsize=1)
def load_sbir_config() -> dict:
    """Return the parsed sbir_config.yaml: agencies, keywords, thresholds."""
    return _read_yaml(_SBIR_PATH)
