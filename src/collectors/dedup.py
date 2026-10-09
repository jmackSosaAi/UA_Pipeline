"""
Cross-source deduplication for the collector pipeline.

Each raw lead gets linked to a canonical_companies row so that the same
company ingested from multiple sources merges into one identity.

  - Exact match on normalized name        → auto-link to existing canonical
  - Token-subset match (≥2 tokens)        → REVIEW flag, skip auto-assign
  - Fuzzy SequenceMatcher match ≥ 0.85   → REVIEW flag, skip auto-assign
  - No match                              → create new canonical, link raw lead
"""

import difflib
import re
import sqlite3

# ---------------------------------------------------------------------------
# Suffix stripping — order matters: longer/dotted forms before short ones
# ---------------------------------------------------------------------------
_SUFFIXES = [
    r"sp\.\s*z\s*o\.o\.",   # Polish
    r"d\.o\.o\.",            # Slovenian/Croatian
    r"s\.a\.",               # French/Spanish (dotted)
    r"b\.v\.",               # Dutch (dotted)
    r"gmbh",
    r"llc",
    r"corp\.",
    r"inc\.",
    r"ltd\.",
    r"limited",
    r"corp",
    r"inc",
    r"ltd",
    r"aps",                  # Danish (before bare "as")
    r"srl",
    r"doo",
    r"oy",
    r"ag",
    r"sl",
    r"ab",
    r"se",
    r"as",
    r"kft",
    r"npc",
    r"sa",                   # plain SA (after dotted s.a.)
    r"bv",                   # plain BV (after dotted b.v.)
    r"oü",                   # Estonian
]

_SUFFIX_RE = re.compile(
    r"[\s,]+\b(" + "|".join(_SUFFIXES) + r")\b[.,]?\s*$",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    """Return a cleaned, lowercased name suitable for dedup comparison."""
    n = name.lower().strip()
    if n.startswith("the "):
        n = n[4:]
    # Strip legal suffixes repeatedly (handles stacked suffixes)
    prev = None
    while prev != n:
        prev = n
        n = _SUFFIX_RE.sub("", n).strip()
    # Collapse whitespace and strip trailing punctuation
    n = re.sub(r"\s+", " ", n).strip(" ,.-")
    return n


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def find_exact_match(normalized_name: str, conn: sqlite3.Connection) -> int | None:
    """Return canonical_id for an exact normalized-name match, or None."""
    row = conn.execute(
        "SELECT id FROM canonical_companies WHERE canonical_name = ?",
        (normalized_name,),
    ).fetchone()
    return row[0] if row else None


def find_token_subset_matches(
    normalized_name: str,
    conn: sqlite3.Connection,
) -> list[tuple[int, str]]:
    """
    Return [(canonical_id, canonical_name)] where the token sets are in a
    strict subset relationship and the shorter name has at least 2 tokens.

    Catches cases like 'pliant energy' ⊂ 'pliant energy systems' that fall
    below the SequenceMatcher threshold.
    """
    tokens = set(normalized_name.split())
    rows = conn.execute("SELECT id, canonical_name FROM canonical_companies").fetchall()
    matches = []
    for cid, cname in rows:
        existing_tokens = set(cname.split())
        shorter = tokens if len(tokens) <= len(existing_tokens) else existing_tokens
        if len(shorter) >= 2 and (tokens < existing_tokens or existing_tokens < tokens):
            matches.append((cid, cname))
    return matches


def find_fuzzy_matches(
    normalized_name: str,
    conn: sqlite3.Connection,
    threshold: float = 0.85,
) -> list[tuple[int, str, float]]:
    """Return [(canonical_id, canonical_name, score)] for fuzzy matches >= threshold."""
    rows = conn.execute("SELECT id, canonical_name FROM canonical_companies").fetchall()
    matches = []
    for cid, cname in rows:
        score = difflib.SequenceMatcher(None, normalized_name, cname).ratio()
        if score >= threshold:
            matches.append((cid, cname, score))
    matches.sort(key=lambda x: x[2], reverse=True)
    return matches


# ---------------------------------------------------------------------------
# Core assignment
# ---------------------------------------------------------------------------


def assign_canonical(
    raw_lead_id: int,
    company_name: str,
    conn: sqlite3.Connection,
) -> int | None:
    """
    Link a raw lead to a canonical company.

    Returns the canonical_id if assigned, None if flagged for review.
    """
    normalized = normalize_name(company_name)

    # 1. Exact match
    canonical_id = find_exact_match(normalized, conn)
    if canonical_id is not None:
        conn.execute(
            "UPDATE raw_leads SET canonical_id = ? WHERE id = ?",
            (canonical_id, raw_lead_id),
        )
        conn.commit()
        return canonical_id

    # 2. Token-subset check — flag for review, do not auto-assign
    subset_matches = find_token_subset_matches(normalized, conn)
    if subset_matches:
        _, best_name = subset_matches[0]
        print(
            f"  REVIEW: '{company_name}' appears to be a subset of '{best_name}' "
            f"— skipping auto-assign"
        )
        return None

    # 3. Fuzzy match — flag for review, do not auto-assign
    fuzzy = find_fuzzy_matches(normalized, conn)
    if fuzzy:
        best_id, best_name, best_score = fuzzy[0]
        print(
            f"  REVIEW: '{company_name}' is similar to '{best_name}' "
            f"(score: {best_score:.2f}) — skipping auto-assign"
        )
        return None

    # 4. No match — create new canonical
    cursor = conn.execute(
        "INSERT INTO canonical_companies (canonical_name) VALUES (?)",
        (normalized,),
    )
    new_id = cursor.lastrowid
    conn.execute(
        "UPDATE raw_leads SET canonical_id = ? WHERE id = ?",
        (new_id, raw_lead_id),
    )
    conn.commit()
    return new_id


# ---------------------------------------------------------------------------
# Batch dedup
# ---------------------------------------------------------------------------


def run_dedup(conn: sqlite3.Connection) -> None:
    """Process all raw_leads with canonical_id IS NULL."""
    rows = conn.execute(
        "SELECT id, company_name FROM raw_leads WHERE canonical_id IS NULL"
    ).fetchall()

    if not rows:
        print("No unlinked raw leads found.")
        return

    print(f"Running dedup on {len(rows)} unlinked raw leads...")

    auto_assigned = new_created = flagged = 0

    for raw_lead_id, company_name in rows:
        normalized = normalize_name(company_name)

        canonical_id = find_exact_match(normalized, conn)
        if canonical_id is not None:
            conn.execute(
                "UPDATE raw_leads SET canonical_id = ? WHERE id = ?",
                (canonical_id, raw_lead_id),
            )
            conn.commit()
            auto_assigned += 1
            continue

        subset_matches = find_token_subset_matches(normalized, conn)
        if subset_matches:
            _, best_name = subset_matches[0]
            print(
                f"  REVIEW: '{company_name}' appears to be a subset of '{best_name}' "
                f"— skipping auto-assign"
            )
            flagged += 1
            continue

        fuzzy = find_fuzzy_matches(normalized, conn)
        if fuzzy:
            best_id, best_name, best_score = fuzzy[0]
            print(
                f"  REVIEW: '{company_name}' is similar to '{best_name}' "
                f"(score: {best_score:.2f}) — skipping auto-assign"
            )
            flagged += 1
            continue

        cursor = conn.execute(
            "INSERT INTO canonical_companies (canonical_name) VALUES (?)",
            (normalized,),
        )
        new_id = cursor.lastrowid
        conn.execute(
            "UPDATE raw_leads SET canonical_id = ? WHERE id = ?",
            (new_id, raw_lead_id),
        )
        conn.commit()
        new_created += 1

    print(f"\n{'=' * 55}")
    print(f"Dedup complete — {len(rows)} raw leads processed")
    print(f"  {auto_assigned:4d}  auto-assigned to existing canonicals")
    print(f"  {new_created:4d}  new canonical companies created")
    print(f"  {flagged:4d}  flagged for manual review")
    print("=" * 55)
