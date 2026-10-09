"""
Loads config/thesis.yaml (public) and config/thesis_sensitive.yaml (gitignored, optional)
and exposes convenience accessors used throughout the pipeline.

If thesis_sensitive.yaml is absent (e.g. fresh clone), all scoring functions work normally
but fund-specific context (team, portfolio, LPs, brigade relationships) will be empty.
"""

import copy
import sys
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG_DIR = Path(__file__).parent.parent / "config"
_THESIS_PATH = _CONFIG_DIR / "thesis.yaml"
_SENSITIVE_PATH = _CONFIG_DIR / "thesis_sensitive.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. override values win on conflict."""
    result = copy.deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


@lru_cache(maxsize=1)
def load() -> dict:
    """Return the merged thesis config. Cached after first call."""
    with open(_THESIS_PATH, encoding="utf-8") as f:
        base = yaml.safe_load(f)

    if _SENSITIVE_PATH.exists():
        with open(_SENSITIVE_PATH, encoding="utf-8") as f:
            sensitive = yaml.safe_load(f)
        if sensitive:
            return _deep_merge(base, sensitive)
    else:
        print(
            "WARNING: config/thesis_sensitive.yaml not found — "
            "scoring will work but without fund-specific context "
            "(team, portfolio, LPs, brigade relationships).",
            file=sys.stderr,
        )

    return base


def fund() -> dict:
    return load()["fund"]


def weights() -> dict[str, float]:
    """Return {dimension_name: weight} for all scoring dimensions."""
    dims = load()["scoring"]["dimensions"]
    return {dim: dims[dim]["weight"] for dim in dims}


def dim_labels() -> dict[str, str]:
    """Return {dimension_name: human_label} derived from thesis dimension names."""
    return {
        "defense_relevance": "Defense relevance",
        "technical_founders": "Technical founders",
        "post_war_durable":   "Post-war durable",
        "nato_exportable":    "NATO-exportable",
        "stage_fit":          "Stage fit",
        "shipped_product":    "Shipped product",
    }


def portfolio_tags() -> dict[str, list[str]]:
    """Return {company_name: [tags]} for all portfolio companies. Empty if sensitive file absent."""
    return {
        c["name"]: c["tags"]
        for c in load().get("portfolio", {}).get("companies", [])
    }


def priority_sectors() -> list[dict]:
    """Return the list of priority sector dicts (name, modifier, keywords, rationale)."""
    return load()["sector_interests"]["priority_sectors"]


def stage_exclusions() -> list[dict]:
    """Return companies listed under stage_exclusions (too late-stage for the fund)."""
    return load().get("stage_exclusions", {}).get("companies", [])


def defense_categories() -> list[dict]:
    """Return the list of defense category dicts (name, short, description, keywords)."""
    return load()["defense_categories"]["categories"]


def tier_thresholds() -> dict:
    """Return the tier_thresholds dict keyed tier_1..tier_4."""
    return load()["defense_categories"]["tier_thresholds"]


def cpv_prefixes() -> list[str]:
    """Return CPV code prefixes used to whitelist ProZorro defense-tech tenders."""
    return load()["procurement_filters"]["cpv_prefixes"]


