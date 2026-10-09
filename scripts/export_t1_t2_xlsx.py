"""Export T1 + T2 sourcing targets to a partner-ready .xlsx.

Three sheets:
  1. Overview — title, definitions, snapshot stats
  2. Tier 1 Targets — ~16 highest-conviction rows
  3. Tier 2 Targets — ~48 strong-candidate rows

Filters out portfolio_company=1 throughout (those are already owned,
not sourcing targets).

Run:
  ./venv/bin/python scripts/export_t1_t2_xlsx.py
Default output: docs/exports/t1_t2_targets_<YYYY-MM-DD>.xlsx
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side,
)
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "companies.db"
DEFAULT_OUT = ROOT / "docs" / "exports" / f"t1_t2_targets_{_dt.date.today():%Y-%m-%d}.xlsx"

# ── Country priority (Ukraine → NATO European → US/Allies → Other) ─────────

_UKRAINE_NAMES = {"ukraine", "ua", "україна"}
_NATO_EUROPEAN = {
    "albania", "belgium", "bulgaria", "croatia", "czech republic", "czechia",
    "denmark", "estonia", "finland", "france", "germany", "greece", "hungary",
    "iceland", "italy", "latvia", "lithuania", "luxembourg", "montenegro",
    "netherlands", "north macedonia", "norway", "poland", "portugal",
    "romania", "slovakia", "slovenia", "spain", "sweden", "turkey", "türkiye",
    "united kingdom", "uk", "great britain", "england",
}
_US_AND_OTHER_ALLIES = {
    "united states", "usa", "us", "united states of america",
    "canada", "australia", "new zealand", "japan", "south korea", "korea, republic of",
    "israel", "singapore",
}


def country_priority(country: str | None) -> int:
    """Lower = appears earlier on the sheet."""
    if not country:
        return 99
    c = country.strip().lower()
    if c in _UKRAINE_NAMES:
        return 0
    if c in _NATO_EUROPEAN:
        return 1
    if c in _US_AND_OTHER_ALLIES:
        return 2
    return 3


# ── Data load ───────────────────────────────────────────────────────────────


def _load_tier_rows(conn: sqlite3.Connection, tier: int) -> list[dict[str, Any]]:
    """Pull a single tier's cohort with portfolio rows filtered out."""
    rows = conn.execute(
        """
        SELECT id, name, hq_country, description, primary_category,
               founded_year, employee_count_est, total_score,
               traction_signals, website, linkedin_url, source,
               dossier_summary, dossier
        FROM companies
        WHERE tier = ?
          AND (portfolio_company IS NULL OR portfolio_company = 0)
          AND (status IS NULL OR status NOT IN ('rejected', 'duplicate'))
        """,
        (tier,),
    ).fetchall()
    return [dict(r) for r in rows]


def _top_founders(conn: sqlite3.Connection, company_id: int, n: int = 3) -> list[str]:
    """Top-N distinct founder names by confidence DESC, then name ASC."""
    rows = conn.execute(
        """
        SELECT name, MAX(confidence) AS c
        FROM founders
        WHERE company_id = ? AND name IS NOT NULL AND TRIM(name) != ''
        GROUP BY name
        ORDER BY c DESC NULLS LAST, name ASC
        LIMIT ?
        """,
        (company_id, n),
    ).fetchall()
    return [r[0] for r in rows]


# ── Field cleaners ──────────────────────────────────────────────────────────


def _clean(value: Any) -> str:
    """Display value as empty for NULL / 'None' / 'N/A' / pure whitespace."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if s.lower() in {"none", "null", "n/a", "na", "<unknown>", "<null>"}:
        return ""
    return s


def _traction_signals_list(raw: Any) -> list[str]:
    """Parse traction_signals JSON array → list of signal strings.

    Defensive against malformed JSON or non-array values."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(v, list):
        return []
    return [str(x) for x in v if x]


def _first_two_sentences(dossier_summary: str | None, dossier: str | None) -> str:
    """Two-sentence "notes" preview.

    Prefer dossier_summary plain text; strip markdown headers (e.g.
    `**OVERVIEW**`) and grab the first two sentences of body content.
    Fall back to parsing the dossier JSON's company_overview.description
    if summary is empty. Returns "" when neither is available.
    """
    text = (dossier_summary or "").strip()
    if not text and dossier:
        try:
            blob = json.loads(dossier)
            text = (blob.get("company_overview") or {}).get("description") or ""
        except (TypeError, ValueError):
            text = ""
    if not text:
        return ""
    # Strip markdown bold headers and section dividers.
    text = re.sub(r"^\s*\*\*[A-Z &/]+\*\*\s*\n+", "", text, flags=re.MULTILINE)
    text = text.replace("---", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Naive sentence split on '. '; rejoin first 2.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    if not parts:
        return ""
    out = " ".join(parts[:2]).strip()
    # Cap at ~600 chars to keep cells readable.
    if len(out) > 600:
        out = out[:600].rsplit(" ", 1)[0] + "…"
    return out


# ── Row assembly ────────────────────────────────────────────────────────────


COLUMNS = [
    "Name", "HQ Country", "Description", "Primary Category",
    "Founded Year", "Employee Count (est.)", "Total Score",
    "Traction Signals", "Top Founders", "Website", "LinkedIn URL",
    "Source", "Notes",
]


def _row_dict(conn: sqlite3.Connection, r: dict[str, Any]) -> dict[str, Any]:
    """Project one DB row to the export row shape."""
    founders = _top_founders(conn, r["id"], n=3)
    signals = _traction_signals_list(r.get("traction_signals"))
    return {
        "Name":              _clean(r.get("name")),
        "HQ Country":        _clean(r.get("hq_country")),
        "Description":       _clean(r.get("description")),
        "Primary Category":  _clean(r.get("primary_category")),
        "Founded Year":      _clean(r.get("founded_year")),
        "Employee Count (est.)": _clean(r.get("employee_count_est")),
        "Total Score":       float(r["total_score"]) if r.get("total_score") is not None else None,
        "Traction Signals":  ", ".join(signals),
        "Top Founders":      ", ".join(founders),
        "Website":           _clean(r.get("website")),
        "LinkedIn URL":      _clean(r.get("linkedin_url")),
        "Source":            _clean(r.get("source")),
        "Notes":             _first_two_sentences(r.get("dossier_summary"), r.get("dossier")),
    }


def _build_tier_frame(conn: sqlite3.Connection, tier: int) -> pd.DataFrame:
    rows = _load_tier_rows(conn, tier)
    records = [_row_dict(conn, r) for r in rows]
    df = pd.DataFrame(records, columns=COLUMNS)
    # Sort: country priority bucket ASC (Ukraine first), then total_score DESC.
    df["_country_priority"] = df["HQ Country"].apply(country_priority)
    df["_score_sort"] = df["Total Score"].fillna(-1.0)
    df = df.sort_values(
        by=["_country_priority", "_score_sort", "Name"],
        ascending=[True, False, True],
    ).drop(columns=["_country_priority", "_score_sort"]).reset_index(drop=True)
    return df


# ── Snapshot stats for Overview ─────────────────────────────────────────────


def _snapshot_stats(conn: sqlite3.Connection) -> dict[str, int]:
    cur = conn.execute(
        "SELECT "
        "  (SELECT COUNT(*) FROM companies) AS total, "
        "  (SELECT COUNT(*) FROM companies WHERE tier = 1 AND (portfolio_company IS NULL OR portfolio_company = 0)) AS t1, "
        "  (SELECT COUNT(*) FROM companies WHERE tier = 2 AND (portfolio_company IS NULL OR portfolio_company = 0)) AS t2, "
        "  (SELECT COUNT(*) FROM companies WHERE tier = 3 AND (portfolio_company IS NULL OR portfolio_company = 0)) AS t3, "
        "  (SELECT COUNT(*) FROM companies WHERE tier = 4 AND (portfolio_company IS NULL OR portfolio_company = 0)) AS t4, "
        "  (SELECT COUNT(*) FROM companies "
        "     WHERE (enriched_at IS NOT NULL OR apify_enriched_at IS NOT NULL) "
        "       AND (portfolio_company IS NULL OR portfolio_company = 0)) AS enriched, "
        "  (SELECT COUNT(DISTINCT company_id) FROM founders "
        "     WHERE name IS NOT NULL AND TRIM(name) != '') AS cos_with_founders, "
        "  (SELECT COUNT(*) FROM companies WHERE portfolio_company = 1) AS portfolio"
    )
    row = cur.fetchone()
    keys = ["total", "t1", "t2", "t3", "t4", "enriched", "cos_with_founders", "portfolio"]
    return dict(zip(keys, row))


# ── openpyxl styling ────────────────────────────────────────────────────────


HEADER_FILL = PatternFill("solid", fgColor="1F3864")  # dark blue
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_ALIGN = Alignment(horizontal="left", vertical="center", wrap_text=True)
ZEBRA_FILL = PatternFill("solid", fgColor="F2F2F2")  # light gray
BORDER = Border(
    left=Side(style="thin", color="D9D9D9"),
    right=Side(style="thin", color="D9D9D9"),
    top=Side(style="thin", color="D9D9D9"),
    bottom=Side(style="thin", color="D9D9D9"),
)
GREEN_FILL  = PatternFill("solid", fgColor="C6EFCE")
YELLOW_FILL = PatternFill("solid", fgColor="FFEB9C")

# Column-name → width (None = auto with cap)
COLUMN_WIDTHS = {
    "Name":                  28,
    "HQ Country":            15,
    "Description":           50,
    "Primary Category":      22,
    "Founded Year":          12,
    "Employee Count (est.)": 16,
    "Total Score":           12,
    "Traction Signals":      28,
    "Top Founders":          30,
    "Website":               30,
    "LinkedIn URL":          35,
    "Source":                18,
    "Notes":                 80,
}
WRAP_COLUMNS = {"Description", "Notes", "Traction Signals", "Top Founders"}


def _bold_when_signal_present(text: str) -> bool:
    """True if the traction-signal text contains a high-emphasis signal."""
    if not text:
        return False
    low = text.lower()
    return "combat_deployed" in low or "military_customer" in low


def _write_tier_sheet(ws: Worksheet, df: pd.DataFrame, sheet_title: str) -> None:
    ws.title = sheet_title
    # Header row
    for col_idx, col_name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = BORDER

    # Data rows
    for r_idx, record in enumerate(df.to_dict("records"), start=2):
        zebra = (r_idx % 2 == 0)
        for c_idx, col in enumerate(COLUMNS, start=1):
            v = record[col]
            cell = ws.cell(row=r_idx, column=c_idx, value=v if v not in ("", None) else None)
            cell.alignment = Alignment(
                horizontal="left", vertical="top",
                wrap_text=(col in WRAP_COLUMNS),
            )
            cell.border = BORDER
            if zebra:
                cell.fill = ZEBRA_FILL
            # Hyperlinks for URL columns
            if col in ("Website", "LinkedIn URL") and isinstance(v, str) and v.startswith(("http://", "https://")):
                cell.hyperlink = v
                cell.font = Font(color="0563C1", underline="single")
            # Bold traction signal cells with high-emphasis tokens
            if col == "Traction Signals" and _bold_when_signal_present(v):
                cell.font = Font(bold=True)
            # Total Score numeric format
            if col == "Total Score" and isinstance(v, (int, float)):
                cell.number_format = "0.00"

    # Conditional formatting on Total Score column
    score_col_idx = COLUMNS.index("Total Score") + 1
    score_letter = get_column_letter(score_col_idx)
    last_row = ws.max_row
    if last_row >= 2:
        rng = f"{score_letter}2:{score_letter}{last_row}"
        # Green for >= 2.5, yellow for 2.0–2.49, blank for < 2.0.
        # Order matters: most-specific (highest threshold) added first.
        ws.conditional_formatting.add(
            rng, CellIsRule(operator="greaterThanOrEqual", formula=["2.5"], fill=GREEN_FILL)
        )
        ws.conditional_formatting.add(
            rng,
            CellIsRule(operator="between", formula=["2.0", "2.499999"], fill=YELLOW_FILL),
        )

    # Column widths
    for c_idx, col in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(c_idx)].width = COLUMN_WIDTHS[col]

    # Freeze header row + autofilter
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    # Row height defaults — openpyxl supports default but content-aware
    # auto-fit isn't really possible without a Calc engine. Leave at
    # default; Excel will auto-grow when the user opens the file.


def _write_overview_sheet(ws: Worksheet, stats: dict[str, int]) -> None:
    ws.title = "Overview"

    # Reasonable column widths
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 80

    row = 1
    def put(value: str, bold: bool = False, size: int = 11, fill: PatternFill | None = None):
        nonlocal row
        c = ws.cell(row=row, column=1, value=value)
        c.font = Font(bold=bold, size=size, color="FFFFFF" if fill is HEADER_FILL else "000000")
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        if fill is not None:
            c.fill = fill
        row += 1

    def kv(label: str, value: Any):
        nonlocal row
        a = ws.cell(row=row, column=1, value=label)
        a.font = Font(bold=True)
        a.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        b = ws.cell(row=row, column=2, value=value)
        b.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        row += 1

    # Title
    title_cell = ws.cell(row=row, column=1, value="Defense Tech Sourcing — T1 & T2 Targets")
    title_cell.font = Font(bold=True, size=16, color="1F3864")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
    row += 1
    ws.cell(row=row, column=1, value=f"Generated: {_dt.date.today():%Y-%m-%d}").font = Font(italic=True, color="595959")
    row += 2

    put("Tier definitions", bold=True, size=13)
    kv("T1 — Top ~16",
       "Highest conviction. Strong defense relevance, multiple traction signals, "
       "thesis-aligned (Ukrainian / NATO European preferred).")
    kv("T2 — ~48",
       "Strong candidates. Clear defense relevance with at least one traction signal.")
    kv("T3 / T4",
       "Long-tail: T3 needs more signal before partner review; T4 is below scoring threshold "
       "or unenriched.")
    row += 1

    put("Traction signal legend", bold=True, size=13)
    kv("combat_deployed",   "Product actively used in current conflict.")
    kv("military_customer", "Has confirmed military / government customer.")
    kv("funding_raised",    "Has raised institutional capital.")
    kv("multi_source",      "Appears in 2+ independent sources (cross-validated).")
    row += 1

    put("Database snapshot", bold=True, size=13)
    kv("Total companies in pipeline",          stats["total"])
    kv("T1 (excluding portfolio)",             stats["t1"])
    kv("T2 (excluding portfolio)",             stats["t2"])
    kv("T3 (excluding portfolio)",             stats["t3"])
    kv("T4 (excluding portfolio)",             stats["t4"])
    kv("Companies with firmographic enrichment", stats["enriched"])
    kv("Companies with founder data captured",   stats["cos_with_founders"])
    kv("Portfolio companies (excluded from sheets)", stats["portfolio"])
    row += 1

    put("Sorting", bold=True, size=13)
    kv("Within each tier",
       "Ukraine first, then NATO European, then US / other Allies, then Other. "
       "Within each country bucket, sorted by total_score (descending).")
    row += 1

    put("Notes", bold=True, size=13)
    kv("Score colour key",
       "Total Score ≥ 2.5 = green. 2.0 – 2.49 = yellow. < 2.0 = no fill.")
    kv("Bold traction signals",
       "Cells containing combat_deployed or military_customer are bolded.")
    kv("Hyperlinks",
       "Website and LinkedIn URL columns are clickable in any modern Excel / Sheets viewer.")


# ── Main ────────────────────────────────────────────────────────────────────


def build_workbook(db_path: Path, output: Path) -> dict[str, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")

    stats = _snapshot_stats(conn)
    t1 = _build_tier_frame(conn, tier=1)
    t2 = _build_tier_frame(conn, tier=2)

    wb = Workbook()
    _write_overview_sheet(wb.active, stats)
    _write_tier_sheet(wb.create_sheet(), t1, "Tier 1 Targets")
    _write_tier_sheet(wb.create_sheet(), t2, "Tier 2 Targets")
    wb.save(output)

    conn.close()
    return {
        "t1_rows": len(t1),
        "t2_rows": len(t2),
        "total":   stats["total"],
        "enriched": stats["enriched"],
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    summary = build_workbook(args.db, args.out)
    size_kb = args.out.stat().st_size / 1024.0
    print(f"Wrote {args.out}")
    print(f"  Tier 1 rows : {summary['t1_rows']}")
    print(f"  Tier 2 rows : {summary['t2_rows']}")
    print(f"  Pipeline total : {summary['total']}")
    print(f"  Enriched cos   : {summary['enriched']}")
    print(f"  File size      : {size_kb:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
