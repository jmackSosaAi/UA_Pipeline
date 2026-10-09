"""
UA Pipeline sourcing pipeline — full end-to-end orchestrator.

    python src/main.py

Steps (each is idempotent — safe to re-run):
  1. ingest  — load companies from active sources (see ingest.SOURCES_CONFIG)
               Brave1 and DIANA are currently disabled (uptime issues).
               Re-enable either by setting its flag to True in SOURCES_CONFIG.
  2. enrich  — fetch homepages, extract structured fields via Claude tool_use
  3. score   — apply weighted thesis scoring (local, no API)
  4. report  — generate per-company LLM narrative, write output/ranked_companies.csv
"""

import csv
import json
import sqlite3
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# Resolve project root regardless of where the script is invoked from
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

import classify  # noqa: E402
import dossier as dossier_gen  # noqa: E402
import enrich  # noqa: E402
import ingest  # noqa: E402
import report  # noqa: E402
import score as scorer  # noqa: E402
import thesis  # noqa: E402
from db.migrate import migrate as migrate_database  # noqa: E402

DB_PATH = ROOT / "data" / "companies.db"
OUTPUT_DIR = ROOT / "output"
CSV_PATH = OUTPUT_DIR / "ranked_companies.csv"

_PARAGRAPH_MODEL = "claude-haiku-4-5"  # swap to claude-opus-4-7 for higher-quality prose

def _validate_thesis() -> None:
    """Load and validate thesis.yaml. Exit with a clear error if anything is wrong."""
    try:
        data = thesis.load()
    except FileNotFoundError:
        sys.exit("ERROR: config/thesis.yaml not found — cannot run pipeline")
    except Exception as exc:
        sys.exit(f"ERROR: failed to parse config/thesis.yaml: {exc}")

    required_keys = {"fund", "portfolio", "scoring", "sector_interests"}
    missing = required_keys - set(data.keys())
    if missing:
        sys.exit(f"ERROR: thesis.yaml missing required sections: {missing}")

    weights = thesis.weights()
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-9:
        sys.exit(f"ERROR: thesis.yaml scoring weights sum to {total:.6f}, must be 1.0")

    dims = set(weights.keys())
    expected = {"defense_relevance", "technical_founders", "post_war_durable",
                "nato_exportable", "stage_fit", "shipped_product"}
    if dims != expected:
        sys.exit(f"ERROR: thesis.yaml dimensions mismatch. Got {dims}, expected {expected}")


_fund = thesis.fund()
_weights = thesis.weights()

_SYSTEM = (
    f"You are a senior investment analyst at {_fund['name']} ({_fund['mission']}), "
    f"a defense-tech venture fund focused on "
    f"{', '.join(_fund['geographic_focus']['tier_1'] + _fund['geographic_focus']['tier_2'])} "
    f"and other NATO-aligned defense companies at the "
    f"{' and '.join(_fund['stage_preference'])} stage. "
    "Write concise, specific assessments for internal deal-flow memos. "
    "Be direct about strengths and risks. Avoid generic VC filler language."
)

_USER_TEMPLATE = """\
Company: {name}
Score: {score:.2f} / 3.00  (rank {rank} of {total})
Website: {website}
Sector: {sector}
Country: {country}
Products: {products}
Technologies: {technologies}
Notable products: {notable}

Dimension scores (each out of 3):
  Defense relevance  {dr_score}/3 (weight 25%) — {dr_reason}
  Technical founders {tf_score}/3 (weight 20%) — {tf_reason}
  Post-war durable   {pw_score}/3 (weight 15%) — {pw_reason}
  NATO-exportable    {n_score}/3  (weight 15%) — {n_reason}
  Stage fit          {sf_score}/3 (weight 15%) — {sf_reason}
  Shipped product    {sp_score}/3 (weight 10%) — {sp_reason}

Overall justification from scoring model: {justification}

Write a single paragraph (2–4 sentences) explaining this company's ranking and its fit \
with the fund's investment thesis. Be specific about the top 1–2 strengths and the main risk \
or limitation. Do not start with the company's name or the word "This".\
"""


# ── DB helpers ────────────────────────────────────────────────────────────────

def _migrate_db(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, col_type in [
        ("llm_paragraph", "TEXT"),
        ("paragraph_at", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
    conn.commit()


def _load_scored(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM companies
        WHERE scored_at IS NOT NULL
          AND enrich_error IS NULL
          AND (portfolio_company IS NULL OR portfolio_company = 0)
        ORDER BY total_score DESC
        """
    ).fetchall()
    results = []
    for row in rows:
        r = dict(row)
        r["_product_types_list"]   = json.loads(r.get("product_types") or "[]")
        r["_technologies_list"]    = json.loads(r.get("technologies") or "[]")
        r["_notable_products_list"]= json.loads(r.get("notable_products") or "[]")
        r["_breakdown"]            = json.loads(r.get("score_breakdown") or "{}")
        r["_matched_sectors"]      = json.loads(r.get("matched_sectors") or "[]")
        results.append(r)
    return results


def _save_paragraph(conn: sqlite3.Connection, company_id: int, paragraph: str) -> None:
    conn.execute(
        "UPDATE companies SET llm_paragraph = ?, paragraph_at = datetime('now') WHERE id = ?",
        (paragraph, company_id),
    )
    conn.commit()


# ── LLM paragraph generation ─────────────────────────────────────────────────

def _build_prompt(row: dict, rank: int, total: int) -> str:
    bd = row["_breakdown"]

    def dim(key: str) -> tuple[int, str]:
        d = bd.get(key, {})
        return d.get("score", 0), d.get("reason", "")

    dr_s, dr_r = dim("defense_relevance")
    tf_s, tf_r = dim("technical_founders")
    pw_s, pw_r = dim("post_war_durable")
    n_s, n_r = dim("nato_exportable")
    sf_s, sf_r = dim("stage_fit")
    sp_s, sp_r = dim("shipped_product")

    return _USER_TEMPLATE.format(
        name=row["name"],
        score=row["total_score"],
        rank=rank,
        total=total,
        website=row["website"] or "—",
        sector=row["primary_sector"] or "unknown",
        country=row["hq_country"] or "unknown",
        products=", ".join(row["_product_types_list"]) or "—",
        technologies=", ".join(row["_technologies_list"]) or "—",
        notable=", ".join(row["_notable_products_list"]) or "—",
        dr_score=dr_s, dr_reason=dr_r,
        tf_score=tf_s, tf_reason=tf_r,
        pw_score=pw_s, pw_reason=pw_r,
        n_score=n_s, n_reason=n_r,
        sf_score=sf_s, sf_reason=sf_r,
        sp_score=sp_s, sp_reason=sp_r,
        justification=row.get("score_justification") or "—",
    )


def generate_paragraphs(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Generate LLM paragraphs for companies that don't already have one."""
    pending = [r for r in rows if not r.get("llm_paragraph")]
    if not pending:
        print("  All paragraphs already generated — skipping API calls.")
        return

    client = anthropic.Anthropic()
    total = len(rows)

    for rank, row in enumerate(rows, 1):
        if row.get("llm_paragraph"):
            print(f"  [{rank}/{total}] {row['name']} — cached, skipping")
            continue

        print(f"  [{rank}/{total}] {row['name']} — generating...")
        prompt = _build_prompt(row, rank, total)

        try:
            response = client.messages.create(
                model=_PARAGRAPH_MODEL,
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            )
            paragraph = next(
                (b.text.strip() for b in response.content if b.type == "text"), ""
            )
        except anthropic.APIError as exc:
            print(f"    Claude error: {exc}")
            paragraph = f"[generation failed: {exc}]"

        row["llm_paragraph"] = paragraph  # update in-memory so CSV picks it up
        _save_paragraph(conn, row["id"], paragraph)
        time.sleep(0.3)


# ── CSV output ────────────────────────────────────────────────────────────────

_BREAKDOWN_KEYS = [
    ("defense_relevance",  "defense"),
    ("technical_founders", "founders"),
    ("post_war_durable",   "post-war"),
    ("nato_exportable",    "NATO"),
    ("stage_fit",          "stage"),
    ("shipped_product",    "shipped"),
]


def _format_breakdown(bd: dict) -> str:
    parts = [f"{label}={bd.get(key, {}).get('score', '?')}/3"
             for key, label in _BREAKDOWN_KEYS]
    return ", ".join(parts)


def _format_thesis_notes(row: dict) -> str:
    """One-line thesis fit summary: sector matches + modifier."""
    matched = row.get("_matched_sectors") or []
    modifier = float(row.get("sector_modifier") or 0)
    parts = []
    if matched:
        parts.append(f"Priority sectors: {', '.join(matched)} (+{modifier:.2f})")
    base = float(row.get("base_score") or 0)
    total = float(row.get("total_score") or 0)
    if modifier == 0:
        parts.append(f"No sector modifier applied (base={base:.2f})")
    else:
        parts.append(f"base={base:.2f} → total={total:.2f} after modifier")
    return " | ".join(parts)


def _format_overlap_warnings(row: dict) -> str:
    """Flag portfolio companies with overlapping product/technology tags."""
    company_tokens = {
        t.lower() for t in
        (row.get("_product_types_list") or []) + (row.get("_technologies_list") or [])
    }
    if not company_tokens:
        return ""

    warnings = []
    for portfolio_name, tags in thesis.portfolio_tags().items():
        portfolio_tokens = {t.lower() for t in tags}
        overlap = set()
        for ptag in portfolio_tokens:
            for ctok in company_tokens:
                if ptag in ctok or ctok in ptag:
                    overlap.add(ptag)
        if overlap:
            warnings.append(f"{portfolio_name} ({', '.join(sorted(overlap))})")
    return "; ".join(warnings)


def _format_category(row: dict) -> str:
    sector = row.get("primary_sector") or "unknown"
    pts = row["_product_types_list"]
    if pts:
        top = ", ".join(pts[:3])
        suffix = " …" if len(pts) > 3 else ""
        return f"{sector} — {top}{suffix}"
    return sector


def write_csv(rows: list[dict]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "rank",
        "name",
        "website",
        "category",
        "product_description",
        "final_score",
        "score_breakdown",
        "thesis_notes",
        "overlap_warnings",
        "llm_explanation",
    ]

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()

        for rank, row in enumerate(rows, 1):
            writer.writerow(
                {
                    "rank":                rank,
                    "name":                row["name"],
                    "website":             row["website"] or "",
                    "category":            _format_category(row),
                    "product_description": row.get("description") or "",
                    "final_score":         f"{row['total_score']:.2f}",
                    "score_breakdown":     _format_breakdown(row["_breakdown"]),
                    "thesis_notes":        _format_thesis_notes(row),
                    "overlap_warnings":    _format_overlap_warnings(row),
                    "llm_explanation":     row.get("llm_paragraph") or "",
                }
            )

    print(f"  Written: {CSV_PATH}")


# ── Pipeline orchestration ────────────────────────────────────────────────────

def _header(step: int, title: str) -> None:
    print(f"\n{'━' * 60}")
    print(f"  Step {step}: {title}")
    print(f"{'━' * 60}")


def main() -> None:
    migrate_database(DB_PATH)
    _validate_thesis()

    _header(1, "Ingest")
    ingest.run()

    _header(2, "Portfolio filter")
    conn_pf = sqlite3.connect(DB_PATH)
    conn_pf.row_factory = sqlite3.Row
    ingest.mark_portfolio_companies(conn_pf)
    conn_pf.close()

    _header(3, "Enrich")
    enrich.run()

    _header(4, "Score")
    scorer.run()

    _header(5, "Generate report")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _migrate_db(conn)

    rows = _load_scored(conn)
    if not rows:
        print("  No scored companies found — pipeline may not have produced results.")
        conn.close()
        return

    print(f"  {len(rows)} companies to report on.")
    print()

    print("  Generating LLM paragraphs...")
    generate_paragraphs(conn, rows)

    print()
    print("  Writing CSV...")
    write_csv(rows)

    conn.close()

    _header(6, "Classify")
    classify.run()

    _header(7, "Category reports")
    report.run()

    print()
    print("━" * 60)
    print("  Pipeline complete.")
    print(f"  Output: {CSV_PATH}")
    print()
    print("  Ranked companies:")
    for rank, row in enumerate(rows, 1):
        tier = "★" if row["total_score"] >= 2.2 else "◆" if row["total_score"] >= 1.5 else "·"
        print(f"    {rank}. {tier} {row['name']:<28} {row['total_score']:.2f}")
    print()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="UA Pipeline sourcing pipeline")
    ap.add_argument("--dossier", action="store_true",
                    help="Run the dossier pipeline on top-N scored companies after the main pipeline")
    ap.add_argument("--dossier-top", type=int, default=10, metavar="N",
                    help="How many top-scored companies to dossier (default: 10)")
    ap.add_argument("--dossier-company", type=str, default=None, metavar="NAME",
                    help="Run dossier for a specific company by name")
    ap.add_argument("--force-refresh", action="store_true",
                    help="Regenerate dossiers even if already present")
    args = ap.parse_args()

    main()

    if args.dossier or args.dossier_company:
        _header(8, "Dossier")
        dossier_gen.run(
            top_n=args.dossier_top,
            company_names=[args.dossier_company] if args.dossier_company else None,
            force_refresh=args.force_refresh,
        )
