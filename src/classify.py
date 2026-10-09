"""
Classifies companies into defense categories defined in thesis.yaml.

Keyword-matched against description, product_types, technologies,
notable_products, primary_sector, and dossier data (when available).
A company can match multiple categories; the one with the most keyword
hits becomes primary, the rest secondary.

Tier assignment uses traction signals (combat deployed, military customer,
funding raised, multi-source validation) plus the score thresholds from
thesis.yaml tier_thresholds.

Run standalone:
    python src/classify.py
"""

import json
import sqlite3
from pathlib import Path

import thesis
from db.migrate import migrate as migrate_database

DB_PATH = Path(__file__).parent.parent / "data" / "companies.db"


# ── DB migration ──────────────────────────────────────────────────────────────

def _migrate_db(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, col_type in [
        ("primary_category",     "TEXT"),
        ("secondary_categories", "TEXT"),   # JSON array
        ("tier",                 "INTEGER"),
        ("tier_label",           "TEXT"),
        ("traction_signals",     "TEXT"),   # JSON array
        ("classified_at",        "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
    conn.commit()


# ── Text extraction ───────────────────────────────────────────────────────────

def _company_text(row: dict) -> str:
    """Concatenate all classifiable fields into one lowercase blob."""
    parts: list[str] = []

    for field in ("name", "description", "primary_sector"):
        if row.get(field):
            parts.append(str(row[field]))

    for json_field in ("product_types", "technologies", "notable_products"):
        try:
            items = json.loads(row.get(json_field) or "[]")
            parts.extend(str(i) for i in items)
        except (json.JSONDecodeError, TypeError):
            pass

    if row.get("dossier"):
        try:
            d = json.loads(row["dossier"])
            pt = d.get("product_technology", {})
            parts.extend(pt.get("product_types", []))
            parts.extend(pt.get("technologies", []))
            desc = d.get("company_overview", {}).get("description")
            if desc:
                parts.append(desc)
        except (json.JSONDecodeError, TypeError):
            pass

    return " ".join(parts).lower()


# ── Classification ────────────────────────────────────────────────────────────

def classify_company(row: dict, categories: list[dict]) -> tuple[str, list[str]]:
    """
    Returns (primary_category, secondary_categories).
    Primary = highest keyword hit count. Secondary = all other matches.
    Returns ("Uncategorized", []) if nothing matches.
    """
    text = _company_text(row)
    if not text.strip():
        return "Uncategorized", []

    scores: dict[str, int] = {}
    for cat in categories:
        hits = sum(1 for kw in cat["keywords"] if kw.lower() in text)
        if hits > 0:
            scores[cat["name"]] = hits

    if not scores:
        return "Uncategorized", []

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    primary   = ranked[0][0]
    secondary = [name for name, _ in ranked[1:]]
    return primary, secondary


# ── Traction signals ──────────────────────────────────────────────────────────

def traction_signals(row: dict) -> list[str]:
    """
    Return a deduplicated list of signal tags present for this company.
    Signals: combat_deployed, military_customer, funding_raised, multi_source.
    Checks dossier JSON first, falls back to flat columns and score_breakdown.
    """
    signals: list[str] = []

    if row.get("dossier"):
        try:
            d = json.loads(row["dossier"])
            pt  = d.get("product_technology", {})
            tv  = d.get("traction_validation", {})
            fin = d.get("financials_funding", {})
            if pt.get("combat_deployed"):
                signals.append("combat_deployed")
            if tv.get("military_customers"):
                signals.append("military_customer")
            if fin.get("total_raised_usd"):
                signals.append("funding_raised")
        except (json.JSONDecodeError, TypeError):
            pass

    # Flat column fallback for funding
    if "funding_raised" not in signals and row.get("funding_amount"):
        signals.append("funding_raised")

    # Combat-deployed inferred from score breakdown (defense_relevance score=3)
    if "combat_deployed" not in signals:
        try:
            bd = json.loads(row.get("score_breakdown") or "{}")
            if bd.get("defense_relevance", {}).get("score", 0) >= 3:
                signals.append("combat_deployed")
        except (json.JSONDecodeError, TypeError):
            pass

    # Multi-source: cross-validated OR appeared in 2+ articles
    mention = int(row.get("mention_count") or 0)
    if row.get("cross_validated") or mention >= 2:
        signals.append("multi_source")

    # deduplicate, preserve order
    return list(dict.fromkeys(signals))


# ── Tier assignment ───────────────────────────────────────────────────────────

def assign_tier(score: float, signals: list[str], thresholds: dict) -> tuple[int, str]:
    """
    Returns (tier_number 1-4, tier_label).
    Tier 1/2 require at least one traction signal in addition to the score gate.
    Tier 3 requires only the score gate.
    Tier 4 is catch-all.
    """
    t1 = thresholds["tier_1"]
    t2 = thresholds["tier_2"]
    t3 = thresholds["tier_3"]
    t4 = thresholds["tier_4"]

    if score >= t1["min_score"] and signals:
        return 1, t1["label"]
    if score >= t2["min_score"] and signals:
        return 2, t2["label"]
    if score >= t3["min_score"]:
        return 3, t3["label"]
    return 4, t4["label"]


# ── Single-company entry point ────────────────────────────────────────────────

def classify_one(company_id: int, conn: sqlite3.Connection) -> tuple[str, list[str]]:
    """Classify and persist a single company. Returns (primary_category, secondary_categories)."""
    _migrate_db(conn)
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        return "Uncategorized", []
    r = dict(row)
    cats       = thesis.defense_categories()
    thresholds = thesis.tier_thresholds()
    primary, secondary = classify_company(r, cats)
    signals    = traction_signals(r)
    score      = float(r.get("total_score") or 0)
    tier_num, tier_lbl = assign_tier(score, signals, thresholds)
    conn.execute(
        """UPDATE companies SET
               primary_category     = ?,
               secondary_categories = ?,
               tier                 = ?,
               tier_label           = ?,
               traction_signals     = ?,
               classified_at        = datetime('now')
           WHERE id = ?""",
        (primary, json.dumps(secondary), tier_num, tier_lbl, json.dumps(signals), company_id),
    )
    conn.commit()
    return primary, secondary


# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> None:
    migrate_database(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _migrate_db(conn)

    cats       = thesis.defense_categories()
    thresholds = thesis.tier_thresholds()

    # Classify every company we have, regardless of enrichment status.
    # Unenriched companies have minimal data and will often end up Uncategorized/T4,
    # but they still appear in the gap analysis rather than disappearing.
    rows = conn.execute(
        """SELECT * FROM companies
           WHERE (portfolio_company IS NULL OR portfolio_company = 0)
             AND (status IS NULL OR status != 'duplicate')"""
    ).fetchall()

    if not rows:
        print("  No companies to classify.")
        conn.close()
        return

    cat_counts: dict[str, int] = {}
    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}

    for row in rows:
        r           = dict(row)
        primary, secondary = classify_company(r, cats)
        signals     = traction_signals(r)
        score       = float(r.get("total_score") or 0)
        tier_num, tier_lbl = assign_tier(score, signals, thresholds)

        conn.execute(
            """UPDATE companies SET
                   primary_category     = ?,
                   secondary_categories = ?,
                   tier                 = ?,
                   tier_label           = ?,
                   traction_signals     = ?,
                   classified_at        = datetime('now')
               WHERE id = ?""",
            (
                primary,
                json.dumps(secondary),
                tier_num,
                tier_lbl,
                json.dumps(signals),
                r["id"],
            ),
        )
        cat_counts[primary] = cat_counts.get(primary, 0) + 1
        tier_counts[tier_num] += 1

    conn.commit()
    conn.close()

    total = sum(cat_counts.values())
    print(f"  Classified {total} companies — "
          f"T1:{tier_counts[1]}  T2:{tier_counts[2]}  "
          f"T3:{tier_counts[3]}  T4:{tier_counts[4]}")
    uncategorized = cat_counts.get("Uncategorized", 0)
    if uncategorized:
        print(f"  ⚠  {uncategorized} companies uncategorized — flag for manual review")
    for name, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        if name != "Uncategorized":
            print(f"      {name}: {count}")


if __name__ == "__main__":
    run()
