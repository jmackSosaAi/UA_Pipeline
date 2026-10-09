import pandas as pd
import streamlit as st

from ui.components import gap_badge, page_title, section_header
from ui.data import _DEFENSE_CATEGORIES, load_companies, load_portfolio_categories
from ui.theme import COLORS


def render(conn=None) -> None:
    df        = load_companies()
    port_cats = load_portfolio_categories()

    page_title("GAP ANALYSIS")

    # ── Category heatmap ──────────────────────────────────────
    section_header("CATEGORY OVERVIEW", margin_top=0)
    _hmap_per_row = 4
    _hmap_rows = []
    for _cat in _DEFENSE_CATEGORIES:
        _cn   = _cat["name"]
        _sh   = _cat.get("short", _cn)
        _cdf  = df[df["primary_category"] == _cn] if not df.empty else pd.DataFrame()
        _n    = len(_cdf)
        _avg  = round(float(_cdf["total_score"].dropna().mean()), 2) if not _cdf.empty and _cdf["total_score"].notna().any() else 0.0
        _n12  = int(_cdf["tier"].isin([1, 2]).sum()) if not _cdf.empty else 0
        _hmap_rows.append({"name": _cn, "short": _sh, "n": _n, "avg": _avg, "n12": _n12})
    for _rs in range(0, len(_hmap_rows), _hmap_per_row):
        _chunk = _hmap_rows[_rs : _rs + _hmap_per_row]
        _hcols = st.columns(_hmap_per_row)
        for _hcol, _hr in zip(_hcols, _chunk):
            _intensity = min(1.0, _hr["avg"] / 3.0)
            _gv        = int(249 - _intensity * 190)
            _bg        = f"rgb({_gv},{_gv},{_gv})"
            _txt       = "#ffffff" if _gv < 130 else COLORS["text"]
            _sub_c     = "rgba(255,255,255,0.65)" if _gv < 130 else COLORS["text_secondary"]
            with _hcol:
                st.markdown(f"""
<div style="background:{_bg};border:1px solid {COLORS["card_border"]};padding:12px;
            margin-bottom:8px;min-height:88px;">
  <div style="font-size:10px;font-weight:800;letter-spacing:0.06em;
              text-transform:uppercase;color:{_sub_c};">{_hr['short']}</div>
  <div style="font-size:20px;font-weight:800;color:{_txt};margin-top:4px;">{_hr['n']}</div>
  <div style="font-size:10px;color:{_sub_c};margin-top:4px;">
    avg {_hr['avg']:.2f} &nbsp;·&nbsp; T1+T2: {_hr['n12']}</div>
</div>""", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    table_rows = []
    for cat in _DEFENSE_CATEGORIES:
        cat_name = cat["name"]
        port_cos = port_cats.get(cat_name, [])
        has_port = bool(port_cos)
        cat_df   = df[df["primary_category"] == cat_name] if not df.empty else pd.DataFrame()
        n        = len(cat_df)
        n1       = int((cat_df["tier"] == 1).sum()) if not cat_df.empty else 0
        n2       = int((cat_df["tier"] == 2).sum()) if not cat_df.empty else 0

        if has_port:
            gap_key = "covered"
        elif n1 + n2 > 0:
            gap_key = "opportunity"
        else:
            gap_key = "open"

        top = ""
        if gap_key == "opportunity":
            cands = cat_df[cat_df["tier"].isin([1, 2])].sort_values("total_score", ascending=False)
            if not cands.empty:
                r = cands.iloc[0]
                top = f'{r["name"]} ({float(r["total_score"]):.2f})'

        table_rows.append({
            "name":      cat_name,
            "n":         n,
            "n1":        n1,
            "n2":        n2,
            "portfolio": ", ".join(port_cos) if port_cos else "—",
            "gap_key":   gap_key,
            "top":       top,
        })

    _order = {"opportunity": 0, "open": 1, "covered": 2}
    table_rows.sort(key=lambda r: _order.get(r["gap_key"], 3))

    leg_cols = st.columns(3)
    for col, (key, label) in zip(leg_cols, [
        ("opportunity", "OPPORTUNITY — No portfolio investment, but T1/T2 candidates identified"),
        ("open",        "OPEN GAP — No portfolio investment and no T1/T2 candidates yet"),
        ("covered",     "COVERED — the fund has at least one portfolio company in this category"),
    ]):
        with col:
            st.markdown(
                f'<div style="margin-bottom:12px;">{gap_badge(key)}'
                f'<span style="font-size:10px;color:{COLORS["text_secondary"]};margin-left:8px;">{label}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    body = ""
    for r in table_rows:
        border_map = {
            "opportunity": f"border-left:3px solid {COLORS['gap_opportunity']};",
            "open":        f"border-left:3px solid {COLORS['gap_open']};",
            "covered":     f"border-left:3px solid {COLORS['gap_covered']};",
        }
        row_style = border_map.get(r["gap_key"], "")
        body += f"""
<tr style="border-bottom:1px solid {COLORS["border_light"]};{row_style}">
  <td style="padding:12px 16px;font-weight:700;font-size:12px;">{r['name']}</td>
  <td style="padding:12px 16px;text-align:center;font-size:12px;font-weight:700;">{r['n']}</td>
  <td style="padding:12px 16px;text-align:center;">
    <span style="font-size:12px;font-weight:800;">{r['n1']}</span>
    <span style="color:{COLORS["text_secondary"]};font-size:12px;"> / {r['n2']}</span>
  </td>
  <td style="padding:12px 16px;font-size:12px;color:#374151;">{r['portfolio']}</td>
  <td style="padding:12px 16px;">{gap_badge(r['gap_key'])}</td>
  <td style="padding:12px 16px;font-size:12px;color:#374151;">{r['top'] or '—'}</td>
</tr>"""

    st.markdown(f"""
<table style="width:100%;border-collapse:collapse;font-family:Inter,sans-serif;">
  <thead>
    <tr style="background:{COLORS["text"]};color:#ffffff;">
      <th style="padding:12px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:0.1em;">CATEGORY</th>
      <th style="padding:12px 16px;text-align:center;font-size:10px;font-weight:700;letter-spacing:0.1em;">TOTAL</th>
      <th style="padding:12px 16px;text-align:center;font-size:10px;font-weight:700;letter-spacing:0.1em;">T1 / T2</th>
      <th style="padding:12px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:0.1em;">PORTFOLIO</th>
      <th style="padding:12px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:0.1em;">STATUS</th>
      <th style="padding:12px 16px;text-align:left;font-size:10px;font-weight:700;letter-spacing:0.1em;">TOP CANDIDATE</th>
    </tr>
  </thead>
  <tbody>{body}</tbody>
</table>
""", unsafe_allow_html=True)
