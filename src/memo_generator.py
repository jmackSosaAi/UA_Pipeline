"""
HTML Investment Memo Generator (generic VC template)
Generates a styled dark-theme HTML memo by feeding company data to Claude
and filling src/templates/memo_template.html.
"""

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import anthropic

import landscape
import thesis

DB_PATH   = Path(__file__).parent.parent / "data" / "companies.db"
OUT_DIR   = Path(__file__).parent.parent / "output" / "memos"
TMPL_PATH = Path(__file__).parent / "templates" / "memo_template.html"

_MODEL    = "claude-sonnet-4-6"

# ── DB migration ──────────────────────────────────────────────────────────────

def _migrate(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, col_type in [
        ("memo",    "TEXT"),   # HTML file path
        ("memo_at", "TEXT"),   # datetime of last generation
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
    conn.commit()


# ── Slug util ─────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ── Data gathering ────────────────────────────────────────────────────────────

def _gather(company_id: int, conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if not row:
        raise ValueError(f"Company {company_id} not found")
    row = dict(row)

    dossier          = json.loads(row.get("dossier") or "{}")
    score_breakdown  = json.loads(row.get("score_breakdown") or "{}")
    traction_signals = json.loads(row.get("traction_signals") or "[]")
    source_urls      = json.loads(row.get("source_urls") or "[]")

    history = [
        dict(r) for r in conn.execute(
            "SELECT total_score, scored_at FROM score_history "
            "WHERE company_id = ? ORDER BY scored_at",
            (company_id,),
        ).fetchall()
    ]

    peers = landscape.similar_companies(company_id, top_n=5, conn=conn)
    peers_data = [
        {
            "name":        p["name"],
            "score":       p["total_score"],
            "tier":        p["tier"],
            "description": (p.get("description") or "")[:200],
            "traction":    p["traction_signals"][:3],
            "hq_country":  p.get("hq_country"),
        }
        for p in peers
    ]

    thesis_data = thesis.load()
    portfolio   = thesis_data["portfolio"]["companies"]
    fund_data   = thesis_data["fund"]

    cat          = row.get("primary_category") or ""
    cat_keywords = next(
        (c.get("keywords", []) for c in thesis_data["defense_categories"]["categories"] if c["name"] == cat),
        [],
    )
    portfolio_overlap = [
        co for co in portfolio
        if any(kw.lower() in " ".join(co["tags"]).lower() for kw in cat_keywords)
    ] if cat_keywords else []

    score = float(row.get("total_score") or 0)
    stage = (
        "Strong Candidate (Tier 1)" if score >= 2.5
        else "Worth Watching (Tier 2)" if score >= 1.8
        else "Early Signal (Tier 3/4)"
    )

    return {
        "company": {
            "id":                   company_id,
            "name":                 row["name"],
            "website":              row.get("website") or "—",
            "description":          row.get("description") or "",
            "primary_category":     cat,
            "secondary_categories": row.get("secondary_categories") or "",
            "hq_country":           row.get("hq_country") or "Unknown",
            "founded_year":         row.get("founded_year"),
            "employee_count":       row.get("employee_count_est"),
            "total_score":          score,
            "score_breakdown":      score_breakdown,
            "tier":                 row.get("tier"),
            "funding_amount":       row.get("funding_amount") or "Undisclosed",
            "traction_signals":     traction_signals,
            "source":               row.get("source"),
            "source_urls":          source_urls[:6],
            "mention_count":        row.get("mention_count") or 0,
            "cross_validated":      bool(row.get("cross_validated")),
            "stage_label":          stage,
            "procurement_value":    row.get("procurement_value"),
            "tender_count":         row.get("tender_count"),
            "investors":            row.get("investors") or "",
        },
        "dossier":          dossier,
        "score_history":    history,
        "peers":            peers_data,
        "portfolio_overlap": portfolio_overlap,
        "all_portfolio":    portfolio,
        "fund": {
            "name":                  fund_data["name"],
            "mission":               fund_data["mission"],
            "check_size_min":        fund_data["check_size_min"],
            "check_size_max":        fund_data["check_size_max"],
            "brigade_relationships": fund_data["brigade_relationships"],
            "stage_preference":      fund_data["stage_preference"],
            "geographic_focus":      fund_data["geographic_focus"],
            "team":                  fund_data["team"],
        },
        "timing": {
            "priority_sectors": [
                {"name": s["name"], "rationale": s["rationale"]}
                for s in thesis.priority_sectors()
            ],
        },
    }


# ── Claude prompt ─────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a senior analyst at Example Fund, an early-stage defense tech fund. "
    "Your writing is direct, evidence-based, and analytically rigorous. You do not soften risks. "
    "You never fabricate metrics, deployment numbers, or funding figures. When information is "
    "unavailable you say so explicitly — 'Undisclosed' or 'Not available' is always correct. "
    "You write investment memos in clear professional prose that a non-technical investor can act on."
)

_PROMPT_TMPL = """\
You are writing a formal investment memo for Example Fund.

The fund is an early-stage defense tech fund.
They invest ${check_min:,}–${check_max:,} at pre-seed/seed in defense tech from Ukraine and allied nations.
Technical co-founders are a non-negotiable requirement.
Operational partners: {brigades}.
Geographic tier-1: Ukraine. Tier-2: Estonia, Germany, Denmark, Poland, UK.

Write a complete investment memo for {name} using the data below. Be direct, specific, and honest.

=== COMPANY DATA ===
{company_json}

=== DOSSIER (extracted from sources) ===
{dossier_json}

=== COMPETITIVE PEERS (same category in the pipeline) ===
{peers_json}

=== CURRENT PORTFOLIO ===
{portfolio_json}

=== PORTFOLIO OVERLAP (same category as subject) ===
{overlap_json}

=== TIMING / PRIORITY SECTORS ===
{timing_json}

=== ANALYST NOTES (incorporate into relevant sections) ===
{user_notes}

---

Generate content for each section. Return ONLY a valid JSON object with these exact keys. \
No markdown fences. No preamble. Just the JSON.

{{
  "recommendation": "2–4 sentences. Must begin with one of: \\"Invest\\", \\"Conditional Invest\\", \\"Watch — Not Yet\\", or \\"Pass\\". Be honest — thin data = Watch. Include specific conditions if Conditional.",

  "subtitle": "PRIMARY_CATEGORY // COUNTRY // STAGE — e.g. \\"Electronic Warfare (EW/SIGINT) // Ukraine // Seed Stage\\"",

  "snapshot": [
    {{"label": "METRIC", "value": "VALUE", "sub": "one-line context", "color": "default|green|orange|yellow"}},
    {{"label": "METRIC", "value": "VALUE", "sub": "one-line context", "color": "default|green|orange|yellow"}},
    {{"label": "METRIC", "value": "VALUE", "sub": "one-line context", "color": "default|green|orange|yellow"}},
    {{"label": "METRIC", "value": "VALUE", "sub": "one-line context", "color": "default|green|orange|yellow"}}
  ],

  "section_01": "HTML using <ul class=\\"blist\\"><li>...</li></ul>. Cover: founding story, HQ, team size if known, core product, key differentiators. Use <strong> for emphasis.",

  "section_02": "HTML using <ul class=\\"blist\\"><li>...</li></ul>. Cover: TAM/SAM with figures if available, growth drivers, structural tailwinds, why now. Use <span class=\\"hl\\"> for key market figures.",

  "section_03": "Traction content. If milestone dates exist in the dossier: use <div class=\\"timeline\\"><div class=\\"tl-item\\"><div class=\\"tl-date\\">DATE</div><div class=\\"tl-dot\\"></div><div class=\\"tl-content\\"><strong>MILESTONE TITLE</strong>Detail text.</div></div></div>. Otherwise use <ul class=\\"blist green\\">.",

  "section_04": "Competitive position. MUST include a <table class=\\"comp-table\\" style=\\"margin-bottom:16px;\\"><thead><tr><th>Attribute</th><th>SUBJECT COMPANY</th><th>COMPETITOR 1</th><th>COMPETITOR 2</th></tr></thead><tbody>...</tbody></table>. Mark the subject company row class=\\"himera-row\\". Use class=\\"win\\" (green), class=\\"lose\\" (red), class=\\"mid\\" (yellow) on individual cells. Follow with <ul class=\\"blist\\"> commentary on positioning.",

  "section_05": "HTML using <ul class=\\"blist\\"><li>...</li></ul>. Team assessment: founder names/backgrounds from dossier, technical depth, domain expertise, relevant prior experience. Be honest about gaps — if data is sparse say so.",

  "section_06": "Product deep dive. Use <div class=\\"two-col\\"><div class=\\"col-block\\"><div class=\\"col-block-title\\">TITLE</div><ul class=\\"blist\\">...</ul></div><div class=\\"col-block\\">...</div></div> if multiple product lines. Cover: key specs, tech stack, production/deployment status, roadmap if known.",

  "section_07": "HTML using <ul class=\\"blist red\\"><li><strong>RISK TITLE</strong> Risk description.<ul class=\\"sub-blist\\"><li>Mitigant: ...</li></ul></li></ul>. Include minimum 4 risks: market, technical/execution, team, and one company-specific. Do not soften.",

  "section_08": "HTML using <ul class=\\"blist yellow\\"><li>...</li></ul>. Portfolio fit: which portfolio companies this complements or competes with, the fund's value-adds that apply, post-war durability vs the thesis, why this fund specifically vs other investors.",

  "section_09": "HTML using <ul class=\\"blist orange\\"><li><strong>QUESTION</strong> Context explaining why this question is critical.</li></ul>. Minimum 5, maximum 8 questions. Must include: unit economics, cap table/governance, key customer concentration, supply chain dependencies, IP ownership, export compliance.",

  "section_10": "Investment thesis HTML. Begin with <div class=\\"ask-amount\\">PROPOSED ENTRY</div><div class=\\"ask-details\\">Context on check size and rationale.</div> followed by <ul class=\\"blist\\"> covering: use of capital, non-capital commitments, follow-on trigger conditions, Series A narrative. If Watch or Pass, explain what must change to become investable."
}}

ABSOLUTE RULES:
- Never fabricate numbers, deployment counts, or funding amounts not present in the data
- Never close an opened HTML tag incorrectly — all HTML must be valid
- All JSON string values must have internal double-quotes escaped as \\\\"
- Return ONLY the JSON object — no markdown fences, no explanation text
"""


def _build_prompt(data: dict, user_notes: str) -> str:
    fund = data["fund"]
    return _PROMPT_TMPL.format(
        check_min=fund["check_size_min"],
        check_max=fund["check_size_max"],
        brigades=", ".join(fund["brigade_relationships"]),
        name=data["company"]["name"],
        company_json=json.dumps(data["company"],          indent=2),
        dossier_json=json.dumps(data["dossier"],          indent=2),
        peers_json=json.dumps(data["peers"],              indent=2),
        portfolio_json=json.dumps(data["all_portfolio"],  indent=2),
        overlap_json=json.dumps(data["portfolio_overlap"], indent=2),
        timing_json=json.dumps(data["timing"],            indent=2),
        user_notes=user_notes or "None provided.",
    )


# ── Template filling ──────────────────────────────────────────────────────────

def _snapshot_cells_html(snapshot: list[dict]) -> str:
    html = ""
    for cell in snapshot[:4]:
        color = cell.get("color", "default")
        cls   = "" if color == "default" else f" {color}"
        html += (
            f'<div class="snap-cell">'
            f'<div class="snap-label">{cell["label"]}</div>'
            f'<div class="snap-value{cls}">{cell["value"]}</div>'
            f'<div class="snap-sub">{cell.get("sub", "")}</div>'
            f'</div>'
        )
    return html


def _user_notes_html(notes: str) -> str:
    if not notes or not notes.strip():
        return ""
    escaped = notes.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<div class="notes-block">'
        '<div class="notes-label">Analyst Notes</div>'
        f'<div class="notes-text">{escaped}</div>'
        '</div>'
    )


def _fill(template: str, sections: dict, data: dict, user_notes: str) -> str:
    company = data["company"]
    today   = date.today().strftime("%B %Y")

    subtitle = sections.get(
        "subtitle",
        f"{company['primary_category']} // {company['hq_country']} // {company['stage_label']}",
    )

    replacements = {
        "company_name":       company["name"].upper(),
        "subtitle":           subtitle,
        "date":               today,
        "recommendation":     sections.get("recommendation", ""),
        "snapshot_cells":     _snapshot_cells_html(sections.get("snapshot", [])),
        "section_01":         sections.get("section_01", ""),
        "section_02":         sections.get("section_02", ""),
        "section_03":         sections.get("section_03", ""),
        "section_04":         sections.get("section_04", ""),
        "section_05":         sections.get("section_05", ""),
        "section_06":         sections.get("section_06", ""),
        "section_07":         sections.get("section_07", ""),
        "section_08":         sections.get("section_08", ""),
        "section_09":         sections.get("section_09", ""),
        "section_10":         sections.get("section_10", ""),
        "user_notes_section": _user_notes_html(user_notes),
        "footer":             "Investment Memo",
    }

    result = template
    for key, value in replacements.items():
        result = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", value, result)
    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_html_memo(
    company_id: int,
    conn: sqlite3.Connection | None = None,
    force_refresh: bool = False,
) -> str:
    """
    Generate a complete HTML investment memo for a company.
    Returns the absolute file path to the generated HTML file in output/memos/.

    Raises ValueError if the company has no dossier.
    Raises anthropic.APIError on Claude failure.
    """
    _close = conn is None
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

    _migrate(conn)

    row = conn.execute(
        "SELECT name, memo, memo_at, dossier, user_notes FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if not row:
        if _close:
            conn.close()
        raise ValueError(f"Company {company_id} not found")

    row      = dict(row)
    out_path = OUT_DIR / f"{_slug(row['name'])}_memo.html"

    # Return cached result if valid
    if not force_refresh and row.get("memo") and out_path.exists():
        if _close:
            conn.close()
        return str(out_path)

    if not row.get("dossier"):
        if _close:
            conn.close()
        raise ValueError(f"{row['name']} has no dossier — run the dossier pipeline first")

    data       = _gather(company_id, conn)
    user_notes = row.get("user_notes") or ""

    client = anthropic.Anthropic()
    prompt = _build_prompt(data, user_notes)

    resp = client.messages.create(
        model=_MODEL,
        max_tokens=8000,
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )

    raw = next((b.text.strip() for b in resp.content if b.type == "text"), "")
    # Strip markdown fences if Claude added them despite instructions
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```\s*$", "", raw)

    sections = json.loads(raw)

    template = TMPL_PATH.read_text(encoding="utf-8")
    html     = _fill(template, sections, data, user_notes)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    conn.execute(
        "UPDATE companies SET memo = ?, memo_at = datetime('now') WHERE id = ?",
        (str(out_path), company_id),
    )
    conn.commit()

    if _close:
        conn.close()

    return str(out_path)
