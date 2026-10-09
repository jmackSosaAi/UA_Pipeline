"""
Pre-enrichment relevance filter for ProZorro companies.

Tags each ProZorro company with relevance_filter:
  "defense_buyer"   — procuring_entity contains a defense/security institution keyword
  "defense_keyword" — company name_latin contains a defense-sector keyword
  "filtered_out"    — neither match; these are skipped by enrich.py

Does NOT delete any rows. Run this once before enriching ProZorro data.

Run:
    python -m src.collectors.prozorro_filter
    python -m src.collectors.prozorro_filter --dry-run
"""

import argparse
import sqlite3
from pathlib import Path

try:
    from db.migrate import migrate as migrate_database
except ImportError:  # supports `python -m src.collectors.prozorro_filter`
    from src.db.migrate import migrate as migrate_database

DB_PATH = Path(__file__).parent.parent.parent / "data" / "companies.db"

# ---------------------------------------------------------------------------
# Match patterns
# ---------------------------------------------------------------------------

# Substrings matched case-insensitively against procuring_entity.
# Any match → "defense_buyer".
_BUYER_PATTERNS: list[str] = [
    # Military units (Ukrainian, various capitalizations and abbreviations)
    "військова частина",
    "військової частини",
    "в/ч",
    "вч а",           # catches "ВЧ А4632" without over-matching "вчитель" etc.
    # Ministry of Defence / General Staff / Armed Forces
    "міністерство оборони",
    "ministry of defense",
    "ministry of defence",
    "генеральний штаб",
    "general staff",
    "збройні сили",
    "armed forces",
    "головне управління зв'язку та кібербезпеки",
    # Border Guard Service
    "державна прикордонна служба",
    "прикордонн",          # catches загін, застава, відділ, академія…
    "border guard",
    # Security Service (SBU) — both nominative and genitive case forms
    "служба безпеки",      # nominative: "Служба безпеки України"
    "служби безпеки",      # genitive: "Центр авіації Служби безпеки України"
    "академія служби безпеки",
    "сбу",
    "sbu",
    "security service",
    # National Guard — both nominative and genitive
    "національна гвардія",
    "національної гвардії",  # genitive: "Медичний центр Національної гвардії України"
    "national guard",
    # State Guard (presidential / VIP protection)
    "управління державної охорони",
    "state guard",
    # Catch-all for any entity with "оборон" (defence, defence ministry dept…)
    "оборон",
    # Intelligence / special ops
    "розвідк",             # розвідка, розвідувальний
    "спеціального призначення",
    "спеціальних операцій",  # anti-terrorism / special ops centers
    "спецназ",
    # State Special Communications Service (SSSCIP / Держспецзв'язку)
    # Both nominative and genitive, plus sub-units
    "державна служба спеціального зв'язку",
    "державної служби спеціального зв'язку",  # genitive form
    "служби спеціального зв'язку",            # short genitive, catches all sub-units
    "департамент забезпечення державної служби спеціального зв'язку",
    "урядового",           # catches "урядового зв'язку" / "урядового фельд'єгерського зв'язку"
    # Compounds / academies / research
    "військово",           # військово-медичний, військово-морський…
    "воєнно",
    "військова академія",
    "науково-дослідний інститут вр",   # Defence ministry research institute
]

# Substrings matched case-insensitively against name_latin.
# Any match → "defense_keyword" (only checked if buyer match fails).
_NAME_KEYWORDS: list[str] = [
    "radio", "radar", "drone", "uav", "military", "defense", "defence",
    "tactical", "armour", "armor", "missile", "optic", "night vision", "thermal",
    "crypto", "cyber", "secure", "signal", "electronic", "aerospace", "aviat",
]


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _buyer_match(entity: str | None) -> bool:
    if not entity:
        return False
    low = entity.lower()
    return any(p in low for p in _BUYER_PATTERNS)


def _name_match(name_latin: str | None) -> bool:
    if not name_latin:
        return False
    low = name_latin.lower()
    return any(k in low for k in _NAME_KEYWORDS)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def _ensure_col(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if "relevance_filter" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN relevance_filter TEXT")
        conn.commit()


# ---------------------------------------------------------------------------
# Core filter
# ---------------------------------------------------------------------------

def filter_relevant_prozorro(
    conn: sqlite3.Connection,
    dry_run: bool = False,
) -> dict[str, list]:
    """
    Tag ProZorro companies with relevance_filter.

    Returns a dict with keys "defense_buyer", "defense_keyword", "filtered_out",
    each containing a list of (id, name, name_latin, procuring_entity) tuples.
    """
    rows = conn.execute(
        "SELECT id, name, name_latin, procuring_entity "
        "FROM companies WHERE source LIKE '%rozorro%'"
    ).fetchall()

    defense_buyer: list[tuple] = []
    defense_keyword: list[tuple] = []
    filtered_out: list[tuple] = []

    for cid, name, name_latin, entity in rows:
        rec = (cid, name, name_latin, entity)
        if _buyer_match(entity):
            defense_buyer.append(rec)
        elif _name_match(name_latin):
            defense_keyword.append(rec)
        else:
            filtered_out.append(rec)

    if not dry_run:
        _ensure_col(conn)
        for cid, *_ in defense_buyer:
            conn.execute(
                "UPDATE companies SET relevance_filter = 'defense_buyer' WHERE id = ?", (cid,)
            )
        for cid, *_ in defense_keyword:
            conn.execute(
                "UPDATE companies SET relevance_filter = 'defense_keyword' WHERE id = ?", (cid,)
            )
        for cid, *_ in filtered_out:
            conn.execute(
                "UPDATE companies SET relevance_filter = 'filtered_out' WHERE id = ?", (cid,)
            )
        conn.commit()

    return {
        "defense_buyer": defense_buyer,
        "defense_keyword": defense_keyword,
        "filtered_out": filtered_out,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Tag ProZorro companies with a relevance_filter value.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be tagged without writing anything.")
    args = ap.parse_args()

    migrate_database(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    result = filter_relevant_prozorro(conn, dry_run=args.dry_run)
    conn.close()

    buyers   = result["defense_buyer"]
    keywords = result["defense_keyword"]
    rejected = result["filtered_out"]
    prefix   = "[DRY RUN] " if args.dry_run else ""

    print(f"\n{prefix}ProZorro relevance filter results")
    print("=" * 60)
    print(f"  {len(buyers):4d}  defense_buyer   (procuring entity matched)")
    print(f"  {len(keywords):4d}  defense_keyword (company name matched)")
    print(f"  {len(rejected):4d}  filtered_out")
    print(f"  {len(buyers)+len(keywords)+len(rejected):4d}  total ProZorro companies")
    print("=" * 60)

    if rejected:
        print(f"\nFiltered-out companies ({len(rejected)}) — sanity check:")
        for cid, name, name_latin, entity in rejected:
            print(f"  [{cid:4d}]  {(name_latin or name or '')[:50]:<52}  buyer: {(entity or '')[:60]}")

    if not args.dry_run:
        print(f"\nTags written to database.")


if __name__ == "__main__":
    main()
