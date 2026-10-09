"""
Utilities for loading and querying the search vocabulary.

The YAML file maps sectors to rich term lists used by collectors and
enrichment scripts to expand web searches beyond narrow keyword matching.
"""

import random
from functools import lru_cache
from pathlib import Path

import yaml

_VOCAB_PATH = Path(__file__).parent / "search_vocabulary.yaml"


@lru_cache(maxsize=1)
def load_vocabulary() -> dict:
    """Return the parsed search_vocabulary.yaml (cached after first load)."""
    with open(_VOCAB_PATH) as f:
        return yaml.safe_load(f)


def get_all_sector_keys() -> list[str]:
    """Return all sector keys defined in the vocabulary."""
    return list(load_vocabulary()["sectors"].keys())


def get_sector_terms(sector_key: str) -> list[str]:
    """Return all terms for a sector (primary_terms + search_terms combined)."""
    sectors = load_vocabulary()["sectors"]
    if sector_key not in sectors:
        raise KeyError(f"Unknown sector: '{sector_key}'. Valid keys: {list(sectors)}")
    sector = sectors[sector_key]
    return sector["primary_terms"] + sector["search_terms"]


def get_search_queries(
    company_name: str,
    sector_hint: str | None = None,
) -> list[str]:
    """
    Generate 8–12 varied search queries for a company.

    Strategy:
      - 2 queries using sector primary terms (if sector_hint provided)
      - 3 queries using sector search terms (if sector_hint provided)
      - Remaining slots filled from general_modifiers
      - Always includes a bare company-name query as the first entry
      - Queries are deduplicated and shuffled (except the bare name stays first)
    """
    vocab = load_vocabulary()
    general = vocab["general_modifiers"]
    sectors = vocab["sectors"]

    queries: list[str] = []

    # Bare name always first — good baseline for any search
    queries.append(company_name)

    sector_primary: list[str] = []
    sector_search: list[str] = []
    if sector_hint and sector_hint in sectors:
        sector_primary = sectors[sector_hint]["primary_terms"]
        sector_search = sectors[sector_hint]["search_terms"]

    # Up to 2 queries from primary sector terms
    for term in random.sample(sector_primary, min(2, len(sector_primary))):
        queries.append(f'"{company_name}" {term}')

    # Up to 3 queries from sector search terms
    for term in random.sample(sector_search, min(3, len(sector_search))):
        queries.append(f'"{company_name}" {term}')

    # Fill remaining slots (target 10 total) from general modifiers
    remaining = 10 - len(queries)
    for mod in random.sample(general, min(remaining, len(general))):
        queries.append(f'"{company_name}" {mod}')

    # Deduplicate while preserving order (bare name stays at index 0)
    seen: set[str] = set()
    deduped: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            deduped.append(q)

    # Return 8–12 queries
    return deduped[:12]


# ---------------------------------------------------------------------------
# Category → sector mapping
# ---------------------------------------------------------------------------

# Exact matches (case-insensitive) checked first.
# Covers DIANA 2026 challenge areas, all distinct primary_category values in the DB,
# and common shorthand variants.
_EXACT_MAP: dict[str, str | None] = {
    # DIANA 2026 challenge areas (official labels)
    "advanced communications":                   "tactical_communications",
    "advanced communication technologies":       "tactical_communications",
    "autonomy and unmanned systems":             "unmanned_aerial_systems",
    "contested electromagnetic spectrum":        "electronic_warfare",
    "contested electromagnetic environments":    "electronic_warfare",
    "contested electromagnetic":                 "electronic_warfare",
    "critical infrastructure and logistics":     "command_control",
    "critical infrastructure":                   "command_control",
    "data and decision making":                  "military_ai_software",
    "data assisted decision making":             "military_ai_software",
    "energy and power":                          "energy_power",
    "human resilience and biotechnology":        None,
    "human resilience":                          "cybersecurity_defense",
    "human performance / medtech":               None,
    "maritime operations":                       "maritime_defense",
    "extreme environments":                      "autonomous_ground",
    "resilient space operations":                "space_defense",
    # DB category label variants (all distinct values observed in the database)
    "electronic warfare":                        "electronic_warfare",
    "electronic warfare (ew/sigint)":            "electronic_warfare",
    "unmanned aerial systems (uas/uav)":         "unmanned_aerial_systems",
    "autonomy & ai software":                    "military_ai_software",
    "space & satellite":                         "space_defense",
    "maritime / naval":                          "maritime_defense",
    "isr & sensor systems":                      "military_ai_software",
    "uav / uas":                                 "unmanned_aerial_systems",
    "communications / ew":                       "tactical_communications",
    "military ai & software":                    "military_ai_software",
    "energy & power":                            "energy_power",
    "c2 / battle management":                    "command_control",
    "unmanned ground systems (ugv/robotics)":    "autonomous_ground",
    "command, control & communications (c4)":    "command_control",
    "unmanned maritime (usv/uuv)":               "maritime_defense",
    "missiles & strike systems":                 None,
    "manufacturing & components":                None,
    "logistics & supply chain":                  None,
    "cybersecurity & information warfare":       "cybersecurity_defense",
    "counter-drone / c-uas":                     "counter_uas",
    # Shorthands
    "ew":                                        "electronic_warfare",
    "c2":                                        "command_control",
    "command and control":                       "command_control",
    "battle management":                         "command_control",
    "isr":                                       "military_ai_software",
    "uav":                                       "unmanned_aerial_systems",
    "uas":                                       "unmanned_aerial_systems",
    "c-uas":                                     "counter_uas",
    "counter-uas":                               "counter_uas",
    "demining/eod":                              "autonomous_ground",
    "demining":                                  "autonomous_ground",
    "eod":                                       "autonomous_ground",
    "cyber":                                     "cybersecurity_defense",
    "space":                                     "space_defense",
    "ai":                                        "military_ai_software",
    "software":                                  "military_ai_software",
    "comms":                                     "tactical_communications",
    "communications":                            "tactical_communications",
    "uncategorized":                             None,
}

# Keyword fragments checked when no exact match found — ordered most-specific first.
# IMPORTANT: more-specific multi-word fragments must precede shorter ones that could
# wrongly pre-empt them (e.g. "unmanned maritime" before "unmanned").
_KEYWORD_MAP: list[tuple[str, str]] = [
    ("electronic warfare",  "electronic_warfare"),   # must precede bare "electronic"
    ("electromagnetic",     "electronic_warfare"),
    ("spectrum",            "electronic_warfare"),
    ("jamming",             "electronic_warfare"),
    ("counter",             "counter_uas"),
    ("anti-drone",          "counter_uas"),
    ("demining",            "autonomous_ground"),
    ("eod",                 "autonomous_ground"),
    ("unmanned maritime",   "maritime_defense"),      # must precede "unmanned"
    ("unmanned ground",     "autonomous_ground"),     # must precede "unmanned"
    ("unmanned",            "unmanned_aerial_systems"),
    ("autonomy",            "unmanned_aerial_systems"),
    ("drone",               "unmanned_aerial_systems"),
    ("uav",                 "unmanned_aerial_systems"),
    ("uas",                 "unmanned_aerial_systems"),
    ("ground robot",        "autonomous_ground"),
    ("ugv",                 "autonomous_ground"),
    ("battle management",   "command_control"),
    ("command",             "command_control"),
    ("c4isr",               "command_control"),
    ("infrastructure",      "command_control"),
    ("communication",       "tactical_communications"),  # matches both "communication" and "communications"
    ("comms",               "tactical_communications"),
    ("radio",               "tactical_communications"),
    ("satellite",           "space_defense"),
    ("space",               "space_defense"),
    ("orbital",             "space_defense"),
    ("maritime",            "maritime_defense"),
    ("naval",               "maritime_defense"),
    ("undersea",            "maritime_defense"),
    ("energy",              "energy_power"),
    ("power",               "energy_power"),
    ("battery",             "energy_power"),
    ("sensor",              "military_ai_software"),
    ("software",            "military_ai_software"),
    ("ai ",                 "military_ai_software"),
    ("data",                "military_ai_software"),
    ("decision",            "military_ai_software"),
    ("intelligence",        "military_ai_software"),
    ("cyber",               "cybersecurity_defense"),
    ("cryptograph",         "cybersecurity_defense"),
    ("information warfare", "cybersecurity_defense"),
]


def map_category_to_sector(category_hint: str | None) -> str | None:
    """
    Map a raw category string (from DIANA, Brave1, etc.) to a vocabulary sector key.

    Returns None if no mapping exists — callers should pass sector_hint=None to
    get_search_queries() in that case, falling back to general modifiers.
    """
    if not category_hint:
        return None

    normalised = category_hint.strip().lower()

    # 1. Exact match
    if normalised in _EXACT_MAP:
        return _EXACT_MAP[normalised]

    # 2. Keyword fragment match (first hit wins)
    for fragment, sector in _KEYWORD_MAP:
        if fragment in normalised:
            return sector

    return None
