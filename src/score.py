"""
Scores each enriched company against the fund's investment thesis.
No API calls — pure logic over the fields already in the database.

Six dimensions, each 0-3 (weights and descriptions live in config/thesis.yaml):
  defense_relevance  — does the product explicitly solve a defense/military problem?
  technical_founders — proxy for technical co-founders with domain expertise (Non-negotiable)
  post_war_durable   — will revenue survive if the active-war phase ends?
  nato_exportable    — domicile / legal structure for frictionless NATO export
  stage_fit          — pre-seed / seed alignment with the fund's mandate
  shipped_product    — evidence of a deployed product (signal, not gate — pre-product OK)

Weights are defined in config/thesis.yaml and must sum to 1.0.
Priority sector modifiers are also defined in thesis.yaml — applied as an additive
bonus after base scoring, capped at 3.0.

Final score range: 0.0 – 3.0.

Run:
    python src/score.py
"""

import json
import sqlite3
from pathlib import Path

import thesis
from db.migrate import migrate as migrate_database

DB_PATH = Path(__file__).parent.parent / "data" / "companies.db"

# Load weights and labels from thesis config — no hardcoded values here.
WEIGHTS = thesis.weights()
_DIM_LABELS = thesis.dim_labels()

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "thesis.yaml scoring weights must sum to 1.0"

# ── Keyword sets ─────────────────────────────────────────────────────────────
_STRIKE_TYPES = {
    "fpv drone", "loitering munition", "strike drone", "kamikaze drone",
    "munition", "attack drone", "anti-armor",
}
_EW_ISR_TYPES = {
    "electronic warfare", "ew", "comint", "elint", "sigint", "counter-drone",
    "direction finding", "jamming", "sdr", "radio monitoring",
}
_UAV_TYPES = {
    "uav", "uas", "unmanned", "drone", "vtol", "fixed-wing", "reconnaissance",
    "surveillance", "isr", "targeting",
}
_PERENNIAL_DEMAND = {
    "comint", "elint", "sigint", "counter-drone", "electronic warfare", "ew",
    "border", "reconnaissance", "surveillance", "isr", "direction finding",
    "radio monitoring", "sdr receiver", "drone detection",
}
_WAR_SPECIFIC = {
    "fpv drone", "loitering munition", "strike drone", "kamikaze",
    "ground attack", "anti-armor",
}
_DEPLOYED_WORDS = {
    "deployed", "combat", "battlefield", "in service", "used by", "frontline",
    "operational", "proven", "combat-proven", "battle-proven", "in action",
    "war", "armed forces",
}
_NATO_MEMBERS = {
    "united states", "united kingdom", "uk", "france", "germany", "poland",
    "canada", "italy", "spain", "netherlands", "norway", "denmark", "belgium",
    "portugal", "greece", "turkey", "czech republic", "slovakia", "hungary",
    "romania", "bulgaria", "albania", "montenegro", "north macedonia",
    "latvia", "lithuania", "estonia", "croatia", "sweden", "finland",
}
_EXPORT_SIGNALS = {
    "export", "nato", "international", "western", "partner", "allied",
    "european", "transatlantic",
}
_MATURE_SIGNALS = {
    "legacy", "established", "leading", "decades", "years of experience",
    "since 19", "since 20", "founded in 19",
}


# ── Per-dimension scorers ────────────────────────────────────────────────────

def _tokens(row: dict) -> tuple[set[str], set[str], set[str], str]:
    """Return lowercased token sets for products, tech, notable products, and description."""
    pt = {p.lower() for p in (row["product_types_list"] or [])}
    te = {t.lower() for t in (row["technologies_list"] or [])}
    np = {p.lower() for p in (row["notable_products_list"] or [])}
    desc = (row["description"] or "").lower()
    return pt, te, np, desc


def _any_match(tokens: set[str], keywords: set[str]) -> bool:
    return any(kw in tok for tok in tokens for kw in keywords)


def score_defense_relevance(row: dict) -> tuple[int, str]:
    pt, te, _, desc = _tokens(row)
    sector = (row["primary_sector"] or "").lower()

    has_strike = _any_match(pt, _STRIKE_TYPES)
    has_ew_isr = _any_match(pt | te, _EW_ISR_TYPES)
    has_uav = _any_match(pt, _UAV_TYPES)
    has_deployed = any(kw in desc for kw in _DEPLOYED_WORDS)

    if sector == "defense" and has_deployed:
        return 3, "combat-deployed or active military customer confirmed"
    if sector == "defense" and (has_strike or has_ew_isr):
        return 3, "core military product (strike / EW / SIGINT)"
    if sector == "defense" and has_uav:
        return 2, "clear defense UAV / ISR platform, deployment not yet confirmed"
    if sector == "defense":
        return 2, "clear defense product, deployment status unconfirmed"
    if sector == "dual-use" and (has_strike or has_ew_isr or has_uav):
        return 1, "dual-use with defense application — defense primacy not confirmed"
    if sector == "dual-use":
        return 1, "dual-use, weak defense specificity"
    return 0, "no defense application identified"


def score_technical_founders(row: dict) -> tuple[int, str]:
    """
    Proxy from homepage: tech stack depth + product specificity.
    Real founder data requires LinkedIn/Crunchbase enrichment — treat these
    scores as lower bounds, not ground truth.
    """
    _, te, _, _ = _tokens(row)
    tech_count = len(row["technologies_list"] or [])
    product_count = len(row["notable_products_list"] or [])

    deep_tech = {
        "sdr", "gnss-denied", "fiber optic", "machine learning", "lrf",
        "eo/ir", "swarm", "gnss", "synthetic aperture", "computer vision",
        "radar", "lidar", "inertial navigation",
    }
    has_deep = _any_match(te, deep_tech)

    if tech_count >= 6 and product_count >= 5:
        return 3, f"deep technical stack ({tech_count} tech items, {product_count} named products)"
    if (tech_count >= 4 or has_deep) and product_count >= 2:
        return 2, f"clear technical depth ({tech_count} tech items) — technical co-founder likely"
    if tech_count >= 2 or product_count >= 1:
        return 1, "some technical evidence; founder background not determinable from homepage"
    return 0, "insufficient technical signal — founder background unknown"


def score_post_war_durable(row: dict) -> tuple[int, str]:
    pt, te, _, _ = _tokens(row)
    all_tokens = pt | te

    has_perennial = _any_match(all_tokens, _PERENNIAL_DEMAND)
    has_war_specific = _any_match(pt, _WAR_SPECIFIC)

    high_durability = _any_match(all_tokens, {
        "counter-drone", "comint", "elint", "sigint",
        "electronic warfare", "ew", "drone detection", "direction finding",
    })

    if high_durability and not has_war_specific:
        return 3, "strong peacetime demand — counter-drone, EW, SIGINT all have civilian/NATO markets"
    if has_perennial and not has_war_specific:
        return 2, "clear dual-use or NATO export path — ISR / surveillance has peacetime market"
    if has_perennial and has_war_specific:
        return 1, "some peacetime application but strike/FPV lines are conflict-dependent"
    if has_war_specific:
        return 0, "revenue primarily tied to active conflict — strike / FPV focus"
    return 1, "durability unclear from available data"


def score_nato_exportable(row: dict) -> tuple[int, str]:
    country = (row["hq_country"] or "").lower().strip()
    desc = (row["description"] or "").lower()

    if country in _NATO_MEMBERS:
        return 3, f"domiciled in NATO member ({row['hq_country']}) — clean procurement path"
    if country in {"eu", "europe"} or any(
        c in country for c in ["austria", "ireland", "switzerland"]
    ):
        return 2, "EU-domiciled — limited export friction, no NATO procurement barrier"
    if country == "ukraine":
        has_export = any(sig in desc for sig in _EXPORT_SIGNALS)
        if has_export:
            return 1, "Ukrainian entity with stated export ambitions"
        return 0, "Ukrainian entity — no export structure evident from homepage"
    if not country:
        return 1, "country unknown — cannot assess export structure"
    return 0, f"non-NATO domicile ({row['hq_country']})"


def score_stage_fit(row: dict) -> tuple[int, str]:
    emp = (row["employee_count_est"] or "").lower()
    desc = (row["description"] or "").lower()
    product_count = len(row["notable_products_list"] or [])

    if "500" in emp or "1000" in emp or "1,000" in emp:
        return 0, "500+ employees — too mature for the fund's seed mandate"
    if "200" in emp or "300" in emp or "400" in emp:
        return 1, "200–400 employees — approaching Series A/B territory"

    has_mature_signal = any(sig in desc for sig in _MATURE_SIGNALS)
    if has_mature_signal:
        return 1, "description signals established company (legacy / multi-decade history)"

    if product_count >= 10:
        return 1, f"{product_count} products implies scaled operations — likely post-seed"
    if product_count >= 5:
        return 2, "mid-size product portfolio consistent with seed / Series A"
    if product_count <= 2:
        return 3, "small product footprint — consistent with pre-seed or seed"

    return 2, "stage signals ambiguous; early indicators present"


def score_shipped_product(row: dict) -> tuple[int, str]:
    _, _, _, desc = _tokens(row)
    product_count = len(row["notable_products_list"] or [])
    has_deployed = any(kw in desc for kw in _DEPLOYED_WORDS)

    if has_deployed and product_count >= 1:
        return 3, f"product combat-deployed or in active military use ({product_count} named)"
    if product_count >= 3:
        return 2, f"{product_count} named products — consistent with field testing / production"
    if product_count >= 1:
        return 1, f"{product_count} named product(s) — prototype or demo level evidence"
    return 0, "no named products found — concept or pre-prototype stage"


# ── Scoring pipeline ─────────────────────────────────────────────────────────

_SCORERS = {
    "defense_relevance":  score_defense_relevance,
    "technical_founders": score_technical_founders,
    "post_war_durable":   score_post_war_durable,
    "nato_exportable":    score_nato_exportable,
    "stage_fit":          score_stage_fit,
    "shipped_product":    score_shipped_product,
}


def _sector_modifier(row: dict) -> tuple[float, list[str]]:
    """Return (total_modifier, [matched_sector_names]) for priority sectors."""
    _, te, _, _ = _tokens(row)
    pt = {p.lower() for p in (row["product_types_list"] or [])}
    all_tokens = pt | te

    total = 0.0
    matched = []
    for sector in thesis.priority_sectors():
        kws = set(sector["keywords"])
        if _any_match(all_tokens, kws):
            total += sector["modifier"]
            matched.append(sector["name"])
    return total, matched


def _enrich_row(row: sqlite3.Row) -> dict:
    r = dict(row)
    r["product_types_list"] = json.loads(r.get("product_types") or "[]")
    r["technologies_list"] = json.loads(r.get("technologies") or "[]")
    r["notable_products_list"] = json.loads(r.get("notable_products") or "[]")
    return r


def score_company(row: dict) -> dict:
    """Return a full scoring result for one company."""
    breakdown: dict[str, dict] = {}
    for dim, fn in _SCORERS.items():
        s, reason = fn(row)
        breakdown[dim] = {"score": s, "reason": reason}

    base_total = sum(WEIGHTS[d] * breakdown[d]["score"] for d in breakdown)

    modifier, matched_sectors = _sector_modifier(row)
    total = min(3.0, round(base_total + modifier, 3))

    # Build justification
    contributions = sorted(
        breakdown.items(),
        key=lambda kv: WEIGHTS[kv[0]] * kv[1]["score"],
        reverse=True,
    )
    top = [(d, v) for d, v in contributions if v["score"] >= 2][:2]
    weak = [(d, v) for d, v in contributions if v["score"] <= 1][:1]

    parts = []
    if top:
        top_str = " and ".join(
            f"{_DIM_LABELS[d].lower()} ({v['score']}/3)" for d, v in top
        )
        parts.append(f"Strong {top_str}")
    if weak:
        d, v = weak[0]
        parts.append(f"limited {_DIM_LABELS[d].lower()} ({v['score']}/3): {v['reason']}")
    if matched_sectors:
        parts.append(f"priority sector match: {', '.join(matched_sectors)} (+{modifier:.2f})")
    if not parts:
        parts.append("mixed signals across all dimensions")

    tier = "strong" if total >= 2.2 else "moderate" if total >= 1.5 else "below-threshold"
    justification = f"{'; '.join(parts)} — {tier} thesis fit."

    return {
        "name": row["name"],
        "total_score": total,
        "base_score": round(base_total, 3),
        "sector_modifier": round(modifier, 3),
        "matched_sectors": matched_sectors,
        "breakdown": breakdown,
        "justification": justification,
    }


# ── Database helpers ─────────────────────────────────────────────────────────

def _migrate_db(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, col_type in [
        ("total_score",        "REAL"),
        ("base_score",         "REAL"),
        ("sector_modifier",    "REAL"),
        ("matched_sectors",    "TEXT"),  # JSON array
        ("score_breakdown",    "TEXT"),  # JSON object
        ("score_justification","TEXT"),
        ("scored_at",          "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS score_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id       INTEGER NOT NULL REFERENCES companies(id),
            total_score      REAL    NOT NULL,
            dimension_scores TEXT    NOT NULL,
            scored_at        TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _save_score(conn: sqlite3.Connection, company_id: int, result: dict) -> None:
    conn.execute(
        """
        UPDATE companies SET
            total_score         = ?,
            base_score          = ?,
            sector_modifier     = ?,
            matched_sectors     = ?,
            score_breakdown     = ?,
            score_justification = ?,
            scored_at           = datetime('now')
        WHERE id = ?
        """,
        (
            result["total_score"],
            result["base_score"],
            result["sector_modifier"],
            json.dumps(result["matched_sectors"]),
            json.dumps(result["breakdown"]),
            result["justification"],
            company_id,
        ),
    )
    conn.execute(
        """INSERT INTO score_history (company_id, total_score, dimension_scores, scored_at)
           VALUES (?, ?, ?, datetime('now'))""",
        (company_id, result["total_score"], json.dumps(result["breakdown"])),
    )
    conn.commit()


# ── Output ────────────────────────────────────────────────────────────────────

def _print_result(result: dict) -> None:
    bar_max = 20
    score = result["total_score"]
    bar_len = int(round(score / 3.0 * bar_max))
    bar = "█" * bar_len + "░" * (bar_max - bar_len)

    print(f"\n{'─' * 60}")
    print(f"  {result['name']}")
    print(f"  Score: {score:.2f} / 3.00  [{bar}]", end="")
    if result["sector_modifier"] > 0:
        print(f"  (+{result['sector_modifier']:.2f} sector: {', '.join(result['matched_sectors'])})")
    else:
        print()
    print()
    for dim, info in result["breakdown"].items():
        label = _DIM_LABELS[dim].ljust(22)
        pip = ("●" * info["score"]) + ("○" * (3 - info["score"]))
        weight_pct = f"(w={WEIGHTS[dim]:.0%})"
        print(f"    {label} {pip}  {info['score']}/3 {weight_pct}")
        print(f"    {'':22} {info['reason']}")
    print()
    print(f"  ↳ {result['justification']}")


def score_one(company_id: int, conn: sqlite3.Connection) -> dict | None:
    """Score and persist a single company. Returns result dict, or None if not found/enriched."""
    _migrate_db(conn)
    # Score-eligible if EITHER legacy enrich.py OR apify_company_enrichment
    # populated the row. enrich_error gate preserved (errored rows still
    # excluded). See docs/DECISIONS.md D-012 for the rescore-chain rationale.
    row = conn.execute(
        """SELECT * FROM companies
            WHERE id = ?
              AND (enriched_at IS NOT NULL OR apify_enriched_at IS NOT NULL)
              AND enrich_error IS NULL""",
        (company_id,),
    ).fetchone()
    if row is None:
        return None
    data = _enrich_row(row)
    result = score_company(data)
    _save_score(conn, company_id, result)
    return result


def run() -> None:
    migrate_database(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _migrate_db(conn)

    # Score-eligible if EITHER legacy enrich.py OR apify_company_enrichment
    # populated the row. enrich_error gate preserved (errored rows still
    # excluded). See docs/DECISIONS.md D-012 for the rescore-chain rationale.
    rows = conn.execute(
        """
        SELECT * FROM companies
        WHERE (enriched_at IS NOT NULL OR apify_enriched_at IS NOT NULL)
          AND enrich_error IS NULL
          AND (portfolio_company IS NULL OR portfolio_company = 0)
          AND (status IS NULL OR status != 'duplicate')
        ORDER BY name
        """
    ).fetchall()

    if not rows:
        print("No enriched companies to score. Run enrich.py first.")
        conn.close()
        return

    _fund = thesis.fund()
    print("\n" + "═" * 60)
    print(f"  {_fund['name'].upper()} — THESIS SCORING REPORT")
    print(f"  Weights: " + "  ".join(
        f"{_DIM_LABELS[d][:8]}={w:.0%}" for d, w in WEIGHTS.items()
    ))
    print("═" * 60)

    results = []
    for row in rows:
        data = _enrich_row(row)
        result = score_company(data)
        results.append((row["id"], result))

    results.sort(key=lambda x: x[1]["total_score"], reverse=True)

    for company_id, result in results:
        _print_result(result)
        _save_score(conn, company_id, result)

    print("\n" + "═" * 60)
    print("  RANKED SUMMARY")
    print("═" * 60)
    for rank, (_, result) in enumerate(results, 1):
        tier_marker = (
            "★" if result["total_score"] >= 2.2 else
            "◆" if result["total_score"] >= 1.5 else "·"
        )
        modifier_str = f"  (+{result['sector_modifier']:.2f})" if result["sector_modifier"] > 0 else ""
        print(f"  {rank}. {tier_marker} {result['name']:<28}  {result['total_score']:.2f} / 3.00{modifier_str}")

    print()
    print("  ★ = strong fit  ◆ = moderate  · = below threshold")
    print()

    conn.close()


if __name__ == "__main__":
    run()
