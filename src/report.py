"""
Generates category-based deal flow reports from classified companies.

Outputs:
  output/deal_flow_by_category.csv  — one row per company, structured columns
  output/category_summaries.md      — tiered company lists per category
  output/category_stats.md          — summary table + gap analysis

Run standalone:
    python src/report.py
"""

import csv
import datetime
import json
import re
import sqlite3
from pathlib import Path

import thesis
from classify import assign_tier, traction_signals
from db.migrate import migrate as migrate_database

DB_PATH    = Path(__file__).parent.parent / "data" / "companies.db"
OUTPUT_DIR = Path(__file__).parent.parent / "output"


# ── Load companies ────────────────────────────────────────────────────────────

def _load_companies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM companies
           WHERE classified_at IS NOT NULL
             AND (portfolio_company IS NULL OR portfolio_company = 0)
           ORDER BY total_score DESC"""
    ).fetchall()
    result = []
    for row in rows:
        r = dict(row)
        r["_product_types"]       = json.loads(r.get("product_types")        or "[]")
        r["_technologies"]        = json.loads(r.get("technologies")          or "[]")
        r["_secondary_categories"]= json.loads(r.get("secondary_categories")  or "[]")
        r["_traction_signals"]    = json.loads(r.get("traction_signals")      or "[]")
        result.append(r)
    return result


# ── Portfolio-to-category mapping ─────────────────────────────────────────────

def _classify_portfolio(categories: list[dict]) -> dict[str, list[str]]:
    """
    Keyword-match each portfolio company against the defense categories.
    A portfolio company can appear in multiple categories (e.g. a swarm-autonomy company → UAS + Autonomy).
    Returns {category_name: [portfolio_company_names, ...]}.
    """
    portfolio = thesis.load()["portfolio"]["companies"]
    result: dict[str, list[str]] = {cat["name"]: [] for cat in categories}
    result["Uncategorized"] = []

    for company in portfolio:
        text = " ".join([
            company.get("name", ""),
            company.get("category", ""),
            " ".join(company.get("tags", [])),
        ]).lower()

        matched: list[str] = []
        for cat in categories:
            hits = sum(1 for kw in cat["keywords"] if kw.lower() in text)
            if hits > 0:
                matched.append(cat["name"])

        if not matched:
            result["Uncategorized"].append(company["name"])
        else:
            for cat_name in matched:
                result[cat_name].append(company["name"])

    return result


# ── Field helpers ─────────────────────────────────────────────────────────────

def _one_liner(row: dict) -> str:
    if row.get("dossier_summary"):
        m = re.search(r"[^.!?\n]+[.!?]", row["dossier_summary"])
        if m:
            return m.group(0).strip()
    if row.get("description"):
        desc = row["description"].strip()
        return desc[:117] + "…" if len(desc) > 120 else desc
    pts = row.get("_product_types", [])
    return ", ".join(pts[:3]) if pts else "—"


def _funding_display(row: dict) -> str:
    if row.get("dossier"):
        try:
            amt = json.loads(row["dossier"]).get("financials_funding", {}).get("total_raised_usd")
            if amt:
                return f"${amt/1_000_000:.1f}M" if amt >= 1_000_000 else f"${amt:,.0f}"
        except (json.JSONDecodeError, TypeError):
            pass
    return str(row["funding_amount"]) if row.get("funding_amount") else ""


def _military_customers(row: dict) -> str:
    if row.get("dossier"):
        try:
            customers = (json.loads(row["dossier"])
                         .get("traction_validation", {})
                         .get("military_customers", []))
            if customers:
                return ", ".join(customers[:3])
        except (json.JSONDecodeError, TypeError):
            pass
    return ""


def _portfolio_overlap(row: dict) -> str:
    if row.get("dossier"):
        try:
            overlaps = (json.loads(row["dossier"])
                        .get("strategic_fit", {})
                        .get("portfolio_overlap", []))
            return ", ".join(overlaps) if overlaps else ""
        except (json.JSONDecodeError, TypeError):
            pass
    return ""


def _source_count(row: dict) -> int:
    if row.get("cross_validated"):
        return max(int(row.get("mention_count") or 0), 2)
    return max(int(row.get("mention_count") or 0), 1)


def _unique_by_id(companies: list[dict]) -> list[dict]:
    seen: set[int] = set()
    result = []
    for c in companies:
        if c["id"] not in seen:
            seen.add(c["id"])
            result.append(c)
    return result


# ── deal_flow_by_category.csv ─────────────────────────────────────────────────

def write_deal_flow_csv(companies: list[dict]) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "deal_flow_by_category.csv"

    fieldnames = [
        "company_name", "primary_category", "secondary_categories",
        "tier", "tier_label", "score",
        "key_products", "funding_raised", "military_customers",
        "portfolio_overlap_warning", "traction_signals",
        "source_count", "dossier_available",
        "website", "country",
    ]

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in companies:
            writer.writerow({
                "company_name":              row["name"],
                "primary_category":          row.get("primary_category") or "Uncategorized",
                "secondary_categories":      ", ".join(row["_secondary_categories"]),
                "tier":                      row.get("tier") or 4,
                "tier_label":                row.get("tier_label") or "Insufficient Data",
                "score":                     f"{float(row.get('total_score') or 0):.2f}",
                "key_products":              ", ".join(row["_product_types"][:4]),
                "funding_raised":            _funding_display(row),
                "military_customers":        _military_customers(row),
                "portfolio_overlap_warning": _portfolio_overlap(row),
                "traction_signals":          ", ".join(row["_traction_signals"]),
                "source_count":              _source_count(row),
                "dossier_available":         "yes" if row.get("dossier_at") else "no",
                "website":                   row.get("website") or "",
                "country":                   row.get("hq_country") or "",
            })

    print(f"  Written: {path.name}  ({len(companies)} rows)")
    return path


# ── category_summaries.md ─────────────────────────────────────────────────────

def _tier_block(companies: list[dict], tier_num: int, tier_label: str) -> list[str]:
    tier_cos = _unique_by_id(
        [c for c in companies if (c.get("tier") or 4) == tier_num]
    )
    tier_cos.sort(key=lambda c: float(c.get("total_score") or 0), reverse=True)
    if not tier_cos:
        return []

    lines = [f"### Tier {tier_num} — {tier_label}"]
    for c in tier_cos:
        score   = float(c.get("total_score") or 0)
        signals = c["_traction_signals"]
        country = c.get("hq_country") or ""
        funding = _funding_display(c)

        badges = []
        if "combat_deployed"  in signals: badges.append("combat-deployed")
        if "military_customer" in signals: badges.append("military customers")
        if funding:                        badges.append(f"raised {funding}")
        if "multi_source"     in signals: badges.append("multi-source")

        parts = [f"- **{c['name']}** ({score:.2f})"]
        if country: parts.append(f"({country})")
        if badges:  parts.append(f"[{', '.join(badges)}]")
        parts.append(f"— {_one_liner(c)}")
        lines.append(" ".join(parts))

    lines.append("")
    return lines


def write_category_summaries(
    companies: list[dict],
    categories: list[dict],
    thresholds: dict,
    portfolio_by_cat: dict[str, list[str]],
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "category_summaries.md"

    tier_labels = {n: thresholds[f"tier_{n}"]["label"] for n in range(1, 5)}

    # Index companies by primary and secondary category
    by_cat: dict[str, list[dict]] = {}
    for c in companies:
        primary = c.get("primary_category") or "Uncategorized"
        by_cat.setdefault(primary, []).append(c)
        for sec in c["_secondary_categories"]:
            by_cat.setdefault(sec, []).append(c)

    today = datetime.date.today()
    lines: list[str] = [
        f"# Deal Flow — By Category",
        f"",
        f"*{today}  ·  {len(companies)} companies  ·  "
        f"T1:{sum(1 for c in companies if (c.get('tier') or 4)==1)}  "
        f"T2:{sum(1 for c in companies if (c.get('tier') or 4)==2)}  "
        f"T3:{sum(1 for c in companies if (c.get('tier') or 4)==3)}  "
        f"T4:{sum(1 for c in companies if (c.get('tier') or 4)==4)}*",
        "",
        "---",
        "",
    ]

    for cat in categories:
        cat_name = cat["name"]
        cos = _unique_by_id(by_cat.get(cat_name, []))
        portfolio_here = portfolio_by_cat.get(cat_name, [])

        count_str = f"{len(cos)} {'company' if len(cos) == 1 else 'companies'}"
        lines.append(f"## {cat_name} ({count_str})")
        lines.append("")

        if portfolio_here:
            lines.append(f"> **Portfolio:** {', '.join(portfolio_here)}")
        else:
            lines.append("> **Portfolio:** none — potential white space")

        t1_count = sum(1 for c in cos if (c.get("tier") or 4) == 1)
        t2_count = sum(1 for c in cos if (c.get("tier") or 4) == 2)
        if not portfolio_here and (t1_count + t2_count) > 0:
            lines.append(
                f"> **Gap flag:** No portfolio investment · "
                f"{t1_count} Tier-1 + {t2_count} Tier-2 candidates identified"
            )
        lines.append("")

        if not cos:
            lines.append("*No companies found in this category.*")
            lines.append("")
            continue

        for tier_num in [1, 2, 3, 4]:
            lines.extend(_tier_block(cos, tier_num, tier_labels[tier_num]))

    # Uncategorized appendix
    uncat = _unique_by_id(by_cat.get("Uncategorized", []))
    if uncat:
        lines.append(f"## Uncategorized ({len(uncat)} companies)")
        lines.append("")
        lines.append("> These companies need manual review to assign a category.")
        lines.append("")
        for c in sorted(uncat, key=lambda x: float(x.get("total_score") or 0), reverse=True):
            score = float(c.get("total_score") or 0)
            lines.append(f"- **{c['name']}** ({score:.2f}) — {_one_liner(c)}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Written: {path.name}  ({len(categories)} categories)")
    return path


# ── category_stats.md ────────────────────────────────────────────────────────

def write_category_stats(
    companies: list[dict],
    categories: list[dict],
    thresholds: dict,
    portfolio_by_cat: dict[str, list[str]],
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "category_stats.md"

    by_cat: dict[str, list[dict]] = {}
    for c in companies:
        primary = c.get("primary_category") or "Uncategorized"
        by_cat.setdefault(primary, []).append(c)

    today = datetime.date.today()
    total          = len(companies)
    with_dossiers  = sum(1 for c in companies if c.get("dossier_at"))
    tier_counts    = {n: sum(1 for c in companies if (c.get("tier") or 4) == n) for n in range(1, 5)}

    t_labels = {n: thresholds[f"tier_{n}"]["label"] for n in range(1, 5)}

    lines: list[str] = [
        "# Deal Flow — Category Statistics",
        "",
        f"*Generated {today}*",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| Total companies in database | {total} |",
        f"| Companies with dossiers | {with_dossiers} |",
        f"| Tier 1 — {t_labels[1]} | {tier_counts[1]} |",
        f"| Tier 2 — {t_labels[2]} | {tier_counts[2]} |",
        f"| Tier 3 — {t_labels[3]} | {tier_counts[3]} |",
        f"| Tier 4 — {t_labels[4]} | {tier_counts[4]} |",
        "",
        "## Category Breakdown",
        "",
        "| Category | Companies | T1 | T2 | T3 | T4 | Portfolio | Priority |",
        "|---|---|---|---|---|---|---|---|",
    ]

    priority_rows: list[tuple[str, int, int]] = []
    gap_rows:      list[str] = []

    for cat in categories:
        name = cat["name"]
        cos  = by_cat.get(name, [])
        t1   = sum(1 for c in cos if (c.get("tier") or 4) == 1)
        t2   = sum(1 for c in cos if (c.get("tier") or 4) == 2)
        t3   = sum(1 for c in cos if (c.get("tier") or 4) == 3)
        t4   = sum(1 for c in cos if (c.get("tier") or 4) == 4)
        pf   = portfolio_by_cat.get(name, [])
        pf_str = ", ".join(pf) if pf else "—"

        if not pf and (t1 + t2) > 0:
            priority = "🔴 HIGH — no portfolio + strong candidates"
            priority_rows.append((name, t1, t2))
        elif not pf and t3 > 0:
            priority = "🟡 MEDIUM — no portfolio + early signals"
        elif not pf and len(cos) == 0:
            priority = "⚪ GAP — no portfolio, no companies found"
            gap_rows.append(name)
        else:
            priority = "🟢 covered"

        lines.append(
            f"| {name} | {len(cos)} | {t1} | {t2} | {t3} | {t4} | {pf_str} | {priority} |"
        )

    uncat_cos = by_cat.get("Uncategorized", [])
    if uncat_cos:
        lines.append(
            f"| Uncategorized | {len(uncat_cos)} | — | — | — | — | — | manual review |"
        )

    lines += [
        "",
        "## Gap Analysis",
        "",
        "### Highest Priority Sourcing Opportunities",
        "*No portfolio investment AND Tier 1 or 2 candidates already identified.*",
        "",
    ]
    if priority_rows:
        priority_rows.sort(key=lambda x: -(x[1] * 2 + x[2]))
        for cat_name, t1, t2 in priority_rows:
            lines.append(f"- **{cat_name}** — {t1} Tier-1, {t2} Tier-2 identified")
    else:
        lines.append("*None — the fund has coverage in all categories with strong candidates.*")

    lines += [
        "",
        "### Market Gaps (No Portfolio Investment, Zero Companies Found)",
        "*These categories are not yet covered by our sources — "
        "worth independent research.*",
        "",
    ]
    if gap_rows:
        for cat_name in gap_rows:
            lines.append(f"- {cat_name}")
    else:
        lines.append("*No gaps — all defined categories have at least one company found.*")

    lines += [
        "",
        "### Portfolio Coverage by Category",
        "",
    ]

    covered = [(cat["name"], portfolio_by_cat.get(cat["name"], []))
               for cat in categories if portfolio_by_cat.get(cat["name"])]
    for cat_name, pf_names in sorted(covered, key=lambda x: -len(x[1])):
        lines.append(f"- **{cat_name}:** {', '.join(pf_names)}")

    uncovered = [cat["name"] for cat in categories
                 if not portfolio_by_cat.get(cat["name"])]
    if uncovered:
        lines += [
            "",
            f"**White space — {len(uncovered)} categories with no portfolio company:**",
            "",
        ]
        for cat_name in uncovered:
            cos  = by_cat.get(cat_name, [])
            t1_n = sum(1 for c in cos if (c.get("tier") or 4) == 1)
            t2_n = sum(1 for c in cos if (c.get("tier") or 4) == 2)
            lines.append(
                f"- {cat_name} — "
                f"{len(cos)} companies found, {t1_n} Tier-1, {t2_n} Tier-2"
            )

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Written: {path.name}")
    return path


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run() -> None:
    migrate_database(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    companies = _load_companies(conn)
    conn.close()

    if not companies:
        print("  No classified companies found — run classify.py first.")
        return

    cats           = thesis.defense_categories()
    thresholds     = thesis.tier_thresholds()
    portfolio_by_cat = _classify_portfolio(cats)

    print(f"  Generating reports for {len(companies)} companies...")
    write_deal_flow_csv(companies)
    write_category_summaries(companies, cats, thresholds, portfolio_by_cat)
    write_category_stats(companies, cats, thresholds, portfolio_by_cat)


if __name__ == "__main__":
    run()
