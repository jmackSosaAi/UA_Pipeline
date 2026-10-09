"""Conservative company entity resolution helpers.

The resolver is intentionally biased toward returning ``None`` unless a match
is clear. Callers can inspect the diagnostic dict to separate true new records
from ambiguous review cases.
"""
from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "companies.db"

SUFFIXES = {
    "ag",
    "as",
    "bv",
    "corp",
    "corporation",
    "gmbh",
    "inc",
    "limited",
    "llc",
    "ltd",
    "nv",
    "oy",
    "sa",
    "sas",
    "spa",
    "srl",
}

GENERIC_FUZZY_TOKENS = {
    "ai",
    "analytics",
    "bio",
    "computing",
    "cyber",
    "data",
    "defense",
    "defence",
    "energy",
    "global",
    "group",
    "imaging",
    "intelligence",
    "labs",
    "networks",
    "power",
    "quantum",
    "robotics",
    "security",
    "solutions",
    "space",
    "systems",
    "tech",
    "technologies",
    "technology",
    "vision",
}

try:  # pragma: no cover - environment dependent
    from rapidfuzz import fuzz as _rapidfuzz_fuzz
except Exception:  # pragma: no cover - fallback is tested
    _rapidfuzz_fuzz = None


def normalize_company_name(name: str | None) -> str:
    """Normalize names for conservative exact-ish matching."""
    if not name:
        return ""
    normalized = name.casefold().strip()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    tokens = [token for token in normalized.split() if token]
    while tokens and tokens[-1].rstrip(".") in SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _normalize_country(country: str | None) -> str:
    return re.sub(r"\s+", " ", (country or "").casefold().strip())


def normalize_website(url: str | None) -> str:
    """Normalize a website URL for cross-source matching.

    Strips scheme (http/https), leading ``www.``, trailing slash, and
    query strings / fragments. Lowercases the whole result. Empty input
    or non-website values (no dot) return ``""``.

    Examples:
      "https://www.Example.com/"        → "example.com"
      "http://example.com?utm=foo"      → "example.com"
      "EXAMPLE.com/path/"               → "example.com/path"
      None                              → ""

    Note: production schema enforces UNIQUE on the *raw* website value,
    so two source rows differing only by trailing slash or www-prefix
    would BOTH attempt to INSERT and the second would hit the
    constraint. Pre-insert match via this normalized form catches those
    cases as merges instead.
    """
    if not url:
        return ""
    s = url.strip().casefold()
    if not s:
        return ""
    # Scheme strip
    for scheme in ("https://", "http://", "//"):
        if s.startswith(scheme):
            s = s[len(scheme):]
            break
    # leading www.
    if s.startswith("www."):
        s = s[4:]
    # query / fragment
    for sep in ("?", "#"):
        i = s.find(sep)
        if i != -1:
            s = s[:i]
    # trailing slash
    s = s.rstrip("/")
    # Sanity: must contain a dot (host.tld) to be a website
    if "." not in s:
        return ""
    return s


def _country_compatible(input_country: str | None, db_country: str | None) -> bool:
    if not input_country or not db_country:
        return True
    return _normalize_country(input_country) == _normalize_country(db_country)


def _token_set_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if _rapidfuzz_fuzz is not None:  # pragma: no cover - rapidfuzz absent locally
        return float(_rapidfuzz_fuzz.token_set_ratio(left, right))
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    shared = " ".join(sorted(left_tokens & right_tokens))
    left_sorted = " ".join(sorted(left_tokens))
    right_sorted = " ".join(sorted(right_tokens))
    scores = [
        SequenceMatcher(None, left_sorted, right_sorted).ratio() * 100,
        SequenceMatcher(None, shared, left_sorted).ratio() * 100 if shared else 0,
        SequenceMatcher(None, shared, right_sorted).ratio() * 100 if shared else 0,
    ]
    return max(scores)


def _fuzzy_allowed(left: str, right: str, score: float) -> bool:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return False
    shared = left_tokens & right_tokens
    distinctive_shared = shared - GENERIC_FUZZY_TOKENS
    if len(left_tokens) == 1 or len(right_tokens) == 1:
        return left == right or SequenceMatcher(None, left, right).ratio() * 100 >= 96
    if len(distinctive_shared) >= 2:
        return True
    if len(distinctive_shared) == 1 and score >= 95:
        return SequenceMatcher(None, left, right).ratio() * 100 >= 70
    if len(shared) >= 2 and score >= 95:
        return SequenceMatcher(None, left, right).ratio() * 100 >= 80
    return False


def _fetch_companies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    # `website` is selected when present so it's available for fuzzy /
    # name passes that read row metadata. Older test fixtures may not
    # have the column; the COALESCE-style approach via PRAGMA is
    # overkill — instead we probe the schema once.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)").fetchall()}
    website_col = ", website" if "website" in cols else ", NULL AS website"
    return conn.execute(
        f"""
        SELECT id, name, name_latin, hq_country, linkedin_url{website_col}
        FROM companies
        WHERE name IS NOT NULL AND TRIM(name) != ''
        """
    ).fetchall()


def _candidate(row: sqlite3.Row, score: float | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "name_latin": row["name_latin"],
        "country": row["hq_country"],
    }
    if score is not None:
        data["score"] = round(score, 2)
    return data


def _diagnostic(match_type: str, candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"match_type": match_type, "candidates": candidates or []}


def match_or_resolve_company(
    name: str,
    country: str | None = None,
    linkedin_url: str | None = None,
    website: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[int | None, dict[str, Any] | None]:
    """Return ``(company_id, diagnostic)`` for a confident match, else ``(None, diagnostic)``.

    ``diagnostic["match_type"]`` is one of:
    - ``linkedin_url``
    - ``website``
    - ``name_latin``
    - ``normalized_name``
    - ``fuzzy``
    - ``ambiguous``
    - ``low_confidence``
    - ``new``

    Match priority (most → least authoritative):
      1. ``linkedin_url`` exact match (already canonicalised upstream)
      2. ``website`` normalised match (handles www-prefix / trailing
         slash / scheme / query-string variation that the schema
         UNIQUE constraint on the raw value cannot)
      3. ``name_latin`` exact (case-insensitive)
      4. ``normalized_name`` exact (suffix-stripped, punctuation-collapsed)
      5. fuzzy token-set ratio with conservative gates
    """
    input_name = (name or "").strip()
    if not input_name:
        return None, _diagnostic("new")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        linkedin = (linkedin_url or "").strip()
        if linkedin:
            rows = conn.execute(
                """
                SELECT id, name, name_latin, hq_country, linkedin_url
                FROM companies
                WHERE linkedin_url = ?
                """,
                (linkedin,),
            ).fetchall()
            rows = [row for row in rows if _country_compatible(country, row["hq_country"])]
            if len(rows) == 1:
                return rows[0]["id"], _diagnostic("linkedin_url", [_candidate(rows[0])])
            if len(rows) > 1:
                return None, _diagnostic("ambiguous", [_candidate(row) for row in rows])

        rows = _fetch_companies(conn)

        # ── Website match (between linkedin_url and name_latin) ──
        # Both source and DB row must have a website; both are
        # normalised (lowercase, scheme/www/query/trailing-slash
        # stripped) before comparison. Country compatibility check
        # applies — protects against the rare case of two real
        # companies sharing the same parent-domain (subsidiaries in
        # different markets).
        normalised_input_website = normalize_website(website)
        if normalised_input_website:
            website_matches = [
                row
                for row in rows
                if normalize_website(row["website"])
                and normalize_website(row["website"]) == normalised_input_website
                and _country_compatible(country, row["hq_country"])
            ]
            if len(website_matches) == 1:
                return website_matches[0]["id"], _diagnostic(
                    "website", [_candidate(website_matches[0])]
                )
            if len(website_matches) > 1:
                return None, _diagnostic(
                    "ambiguous", [_candidate(row) for row in website_matches]
                )

        input_latin = input_name.casefold()
        latin_matches = [
            row
            for row in rows
            if row["name_latin"]
            and row["name_latin"].strip().casefold() == input_latin
            and _country_compatible(country, row["hq_country"])
        ]
        if len(latin_matches) == 1:
            return latin_matches[0]["id"], _diagnostic("name_latin", [_candidate(latin_matches[0])])
        if len(latin_matches) > 1:
            return None, _diagnostic("ambiguous", [_candidate(row) for row in latin_matches])

        normalized_input = normalize_company_name(input_name)
        normalized_matches = [
            row
            for row in rows
            if normalize_company_name(row["name"]) == normalized_input
            and _country_compatible(country, row["hq_country"])
        ]
        if len(normalized_matches) == 1:
            return normalized_matches[0]["id"], _diagnostic(
                "normalized_name", [_candidate(normalized_matches[0])]
            )
        if len(normalized_matches) > 1:
            return None, _diagnostic("ambiguous", [_candidate(row) for row in normalized_matches])

        scored: list[tuple[sqlite3.Row, float]] = []
        for row in rows:
            if not _country_compatible(country, row["hq_country"]):
                continue
            row_names = [row["name"]]
            if row["name_latin"]:
                row_names.append(row["name_latin"])
            candidate_scores = [
                (
                    _token_set_ratio(normalized_input, normalize_company_name(value)),
                    normalize_company_name(value),
                )
                for value in row_names
            ]
            score, candidate_name = max(candidate_scores, key=lambda item: item[0])
            if score >= 70 and _fuzzy_allowed(normalized_input, candidate_name, score):
                scored.append((row, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        high_confidence = [(row, score) for row, score in scored if score >= 85]
        if len(high_confidence) == 1:
            row, score = high_confidence[0]
            return row["id"], _diagnostic("fuzzy", [_candidate(row, score)])
        if len(high_confidence) > 1:
            best_score = high_confidence[0][1]
            best = [(row, score) for row, score in high_confidence if best_score - score < 5]
            if len(best) == 1:
                row, score = best[0]
                return row["id"], _diagnostic("fuzzy", [_candidate(row, score)])
            return None, _diagnostic("ambiguous", [_candidate(row, score) for row, score in high_confidence[:5]])
        if scored:
            return None, _diagnostic("low_confidence", [_candidate(row, score) for row, score in scored[:5]])
        return None, _diagnostic("new")
    finally:
        conn.close()
