import json
import sqlite3
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import landscape as _landscape
from ui.components import (
    category_badge,
    confidence_dot,
    confidence_tag,
    contact_row,
    force_scroll_top,
    founder_card,
    gap_badge,
    page_title,
    radar_chart,
    section_header,
    slug,
    stat_html,
    tier_badge,
)
from ui.theme import COLORS
from ui.data import (
    DB_PATH,
    _DEFENSE_CATEGORIES,
    _PORTFOLIO_TAGS,
    _read,
    _write,
    compute_buzz_score,
    load_companies,
    load_company,
    load_company_press_mentions,
    load_portfolio_categories,
    load_recent_mention_counts,
    load_score_history,
    load_score_trends,
    DIM_LABELS,
    STATUS_LABELS,
    STATUS_STAGES,
)


# ── Buzz score helpers ────────────────────────────────────────────────────────


def _buzz_color(mentions: int) -> str:
    if mentions >= 5:
        return COLORS["gap_opportunity"]   # hot — orange
    if mentions >= 2:
        return "#2563eb"                    # warm — blue
    return COLORS["text_muted"]             # cold — gray


def _buzz_badge_html(buzz: dict) -> str:
    """Pill summarizing recent press attention. Tooltip shows full breakdown."""
    n = int(buzz.get("mention_count") or 0)
    pubs = int(buzz.get("publication_count") or 0)
    avg = float(buzz.get("avg_relevance") or 0.0)
    trend = buzz.get("trend") or "steady"
    color = _buzz_color(n)
    title = (
        f"{n} mention{'s' if n != 1 else ''} across {pubs} publication"
        f"{'s' if pubs != 1 else ''} · avg relevance {avg:.2f} · trend: {trend}"
    )
    return (
        f'<span title="{title}" style="background:{color};color:#ffffff;'
        f'padding:4px 10px;font-size:10px;font-weight:700;letter-spacing:0.06em;'
        f'font-family:Inter,sans-serif;">'
        f'BUZZ: {n} MENTIONS / 30D</span>'
    )


def render(conn=None, company_id: int | None = None) -> None:
    if company_id is not None:
        _render_detail(company_id)
    else:
        _render_overview()


# ─────────────────────────────────────────────────────────────────────────────

def _render_overview() -> None:
    df_all    = load_companies()
    trends    = load_score_trends()
    port_cats = load_portfolio_categories()

    show_unenriched = st.session_state.get("ov_show_unenriched", False)
    _has_data = (
        df_all["description"].notna() |
        df_all["enriched_at"].notna() |
        df_all["dossier_at"].notna()
    ) if not df_all.empty else pd.Series(dtype=bool)
    _n_enriched = int(_has_data.sum()) if not df_all.empty else 0
    _n_empty    = len(df_all) - _n_enriched
    df = df_all[_has_data] if (not show_unenriched and not df_all.empty) else df_all

    page_title("DEAL FLOW OVERVIEW")

    total    = len(df)
    t1       = int((df["tier"] == 1).sum()) if not df.empty else 0
    t2       = int((df["tier"] == 2).sum()) if not df.empty else 0
    dossiers = int(df["dossier_at"].notna().sum()) if not df.empty else 0

    gaps = 0
    if not df.empty:
        for cat in _DEFENSE_CATEGORIES:
            has_port  = bool(port_cats.get(cat["name"]))
            cat_slice = df[df["primary_category"] == cat["name"]]
            if not has_port and cat_slice["tier"].isin([1, 2]).any():
                gaps += 1

    cols = st.columns(5)
    for col, (lbl, val, note) in zip(cols, [
        ("Total Companies",   total,    None),
        ("Tier 1 — Strong",   t1,       "Score ≥ 2.5"),
        ("Tier 2 — Watching", t2,       "Score ≥ 1.8"),
        ("Dossiers",          dossiers, "Full analyses"),
        ("Portfolio Gaps",    gaps,     "No the fund + T1/T2 exists"),
    ]):
        with col:
            st.markdown(stat_html(lbl, val, note or ""), unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    section_header("CATEGORIES", margin_top=0)
    per_row = 4
    for row_start in range(0, len(_DEFENSE_CATEGORIES), per_row):
        chunk = _DEFENSE_CATEGORIES[row_start : row_start + per_row]
        cols  = st.columns(per_row)
        for col, cat in zip(cols, chunk):
            cat_name  = cat["name"]
            short     = cat.get("short", cat_name)
            port_cos  = port_cats.get(cat_name, [])
            has_port  = bool(port_cos)
            cat_df    = df[df["primary_category"] == cat_name] if not df.empty else pd.DataFrame()
            n         = len(cat_df)
            n1        = int((cat_df["tier"] == 1).sum()) if not cat_df.empty else 0
            n2        = int((cat_df["tier"] == 2).sum()) if not cat_df.empty else 0
            is_opp    = not has_port and (n1 + n2) > 0

            border     = COLORS["gap_opportunity"] if is_opp else (COLORS["gap_covered"] if has_port else COLORS["card_border"])
            opp_html   = f'<div style="margin-top:8px;">{gap_badge("opportunity")}</div>' if is_opp else ""
            port_html  = ""
            if has_port:
                names = ", ".join(port_cos[:2]) + ("…" if len(port_cos) > 2 else "")
                port_html = (
                    f'<div style="font-size:10px;color:{COLORS["text_secondary"]};margin-top:4px;'
                    f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
                    f'{names}</div>'
                )

            with col:
                st.markdown(f"""
<div style="background:{COLORS["card_bg"]};border:1px solid {border};padding:12px;
            margin-bottom:8px;min-height:105px;">
  <div style="font-size:10px;font-weight:800;letter-spacing:0.06em;
              text-transform:uppercase;color:{COLORS["text"]};">{short}</div>
  <div style="font-size:26px;font-weight:800;color:{COLORS["text"]};margin-top:4px;">{n}</div>
  <div style="font-size:10px;color:{COLORS["text_secondary"]};margin-top:4px;">T1:{n1} &nbsp; T2:{n2}</div>
  {port_html}
  {opp_html}
</div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    section_header("TOP COMPANIES", margin_top=0)
    top_df = df[pd.notna(df["total_score"])].sort_values("total_score", ascending=False)
    top_df = top_df[top_df["total_score"] >= 1.8].head(12)

    if top_df.empty:
        st.markdown(
            f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:8px 0 16px;">No scored companies yet.</div>',
            unsafe_allow_html=True,
        )
    else:
        mention_counts = load_recent_mention_counts(days=30)
        n_card_cols = 4
        for row_start in range(0, len(top_df), n_card_cols):
            chunk     = top_df.iloc[row_start : row_start + n_card_cols]
            card_cols = st.columns(n_card_cols)
            for col, (_, r) in zip(card_cols, chunk.iterrows()):
                cid       = int(r["id"])
                score_val = float(r["total_score"])
                tier_val  = int(r["tier"]) if pd.notna(r.get("tier")) else 4
                tier_bg   = {1:COLORS["tier1"], 2:COLORS["tier2"], 3:COLORS["tier3"], 4:COLORS["tier4"]}.get(tier_val, COLORS["tier4"])
                tier_txt  = "#ffffff" if tier_val < 4 else COLORS["text"]
                cat_str   = (r.get("primary_category") or "—")[:28]
                status_str = STATUS_LABELS.get(r.get("status"), r.get("status") or "Sourced")
                desc_str   = (r.get("description") or "")[:80] + ("…" if len(r.get("description") or "") > 80 else "")
                dos_mark   = "✓ Dossier" if r.get("dossier_at") else "○ No dossier"
                dos_color  = COLORS["gap_covered"] if r.get("dossier_at") else COLORS["text_muted"]
                m_count    = mention_counts.get(cid, 0)
                buzz_html  = (
                    f'<div title="{m_count} press mentions in the last 30 days" '
                    f'style="font-size:10px;color:{_buzz_color(m_count)};margin-top:4px;'
                    f'font-weight:700;letter-spacing:0.04em;">'
                    f'📰 {m_count} mention{"s" if m_count != 1 else ""} / 30d</div>'
                ) if m_count > 0 else ""
                with col:
                    st.markdown(f"""
<div class="ua-card" style="background:{COLORS["card_bg"]};border:1px solid {COLORS["card_border"]};padding:16px;
            margin-bottom:0;min-height:160px;display:flex;flex-direction:column;">
  <div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap;">
    <span style="background:{tier_bg};color:{tier_txt};padding:4px 8px;
                 font-size:10px;font-weight:700;letter-spacing:0.06em;">T{tier_val}</span>
    <span style="background:{COLORS["text"]};color:#ffffff;padding:4px 8px;
                 font-size:10px;font-weight:700;letter-spacing:0.04em;">{cat_str}</span>
  </div>
  <div style="font-size:12px;font-weight:800;letter-spacing:0.04em;
              text-transform:uppercase;color:{COLORS["text"]};line-height:1.3;margin-bottom:4px;">{r["name"]}</div>
  <div style="font-size:26px;font-weight:800;color:{COLORS["text"]};line-height:1;">{score_val:.2f}</div>
  <div style="font-size:10px;color:{COLORS["text_secondary"]};margin-top:4px;">{status_str}</div>
  <div style="font-size:10px;color:#374151;margin-top:8px;line-height:1.4;flex:1;">{desc_str}</div>
  {buzz_html}
  <div style="font-size:10px;color:{dos_color};margin-top:8px;font-weight:600;">{dos_mark}</div>
</div>""", unsafe_allow_html=True)
                    if st.button("VIEW →", key=f"top_card_{cid}", use_container_width=True):
                        st.session_state.selected_company_id = cid
                        st.session_state.page = "detail"
                        st.session_state.nav_origin = None
                        st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    section_header("COMPANY TABLE", margin_top=0)

    if df.empty:
        st.info("No companies found.")
        return

    tier_f = st.session_state.get("ov_tier", "All")
    cat_f  = st.session_state.get("ov_cat",  "All")
    src_f  = st.session_state.get("ov_src",  "All")
    st_f   = st.session_state.get("ov_status","All")

    filt = df.copy()
    if tier_f != "All":
        filt = filt[filt["tier"] == tier_f]
    if cat_f  != "All":
        filt = filt[filt["primary_category"] == cat_f]
    if src_f  != "All":
        filt = filt[filt["source"] == src_f]
    if st_f   != "All":
        filt = filt[filt["status"] == st_f]

    rows_out = []
    for _, r in filt.iterrows():
        cid     = r["id"]
        signals = len(json.loads(r.get("traction_signals") or "[]"))
        rows_out.append({
            "_id":      cid,
            "Company":  r["name"],
            "Category": r.get("primary_category") or "—",
            "Tier":     f"T{int(r['tier'])}" if pd.notna(r.get("tier")) else "—",
            "Score":    round(float(r["total_score"]), 2) if pd.notna(r.get("total_score")) else None,
            "Trend":    trends.get(cid, "→"),
            "Status":   STATUS_LABELS.get(r.get("status"), r.get("status") or "—"),
            "Signals":  signals,
            "Funding":  r.get("funding_amount") or "—",
            "Dossier":  "✓" if r.get("dossier_at") else "✗",
        })
    disp = pd.DataFrame(rows_out)

    _hidden_note = (
        f' &nbsp;·&nbsp; {_n_empty} unenriched hidden'
        if not show_unenriched and _n_empty > 0
        else (f' &nbsp;·&nbsp; {_n_enriched} with data, {_n_empty} empty' if show_unenriched else "")
    )
    st.markdown(
        f'<div style="font-size:10px;color:{COLORS["text_secondary"]};margin-bottom:8px;">'
        f'{len(disp)} companies{_hidden_note}</div>',
        unsafe_allow_html=True,
    )

    with st.expander(f"FULL LIST — {len(disp)} companies", expanded=False):
        if not disp.empty:
            event = st.dataframe(
                disp.drop(columns=["_id"]),
                use_container_width=True,
                hide_index=True,
                height=500,
                on_select="rerun",
                selection_mode="single-row",
                column_config={
                    "Score":   st.column_config.NumberColumn("Score",   format="%.2f"),
                    "Signals": st.column_config.NumberColumn("Signals", format="%d"),
                    "Tier":    st.column_config.TextColumn("Tier"),
                    "Trend":   st.column_config.TextColumn("Trend"),
                    "Dossier": st.column_config.TextColumn("Dossier"),
                },
                key="company_table",
            )
            if event.selection.rows:
                idx = event.selection.rows[0]
                cid = int(disp.iloc[idx]["_id"])
                st.session_state.selected_company_id = cid
                st.session_state.page = "detail"
                st.session_state.nav_origin = None
                st.rerun()

    section_header("DEAL FLOW INTELLIGENCE")
    geo_l, geo_r = st.columns(2)
    with geo_l:
        st.markdown(
            '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
            f'text-transform:uppercase;color:{COLORS["text_secondary"]};margin-bottom:8px;">Companies by Source</div>',
            unsafe_allow_html=True,
        )
        if not filt.empty and filt["source"].notna().any():
            src_counts = filt["source"].value_counts().reset_index()
            src_counts.columns = ["Source", "Count"]
            src_chart = (
                alt.Chart(src_counts)
                .mark_bar(color="#374151")
                .encode(
                    x=alt.X("Count:Q", axis=alt.Axis(labelFontSize=9, title=None)),
                    y=alt.Y("Source:N", sort="-x",
                            axis=alt.Axis(labelFontSize=10, title=None)),
                    tooltip=["Source", "Count"],
                )
                .properties(height=max(60, len(src_counts) * 28), background="white")
                .configure_view(strokeWidth=0, fill="white")
                .configure_axis(grid=False, labelColor="#111111", titleColor="#111111")
            )
            st.altair_chart(src_chart, use_container_width=True)
        else:
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_muted"]};">No source data available.</div>',
                unsafe_allow_html=True,
            )
    with geo_r:
        st.markdown(
            '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
            f'text-transform:uppercase;color:{COLORS["text_secondary"]};margin-bottom:8px;">Companies by Country</div>',
            unsafe_allow_html=True,
        )
        if not filt.empty and filt["hq_country"].notna().any():
            ctry_counts = filt["hq_country"].dropna().value_counts().reset_index()
            ctry_counts.columns = ["Country", "Count"]
            ctry_chart = (
                alt.Chart(ctry_counts.head(10))
                .mark_bar(color="#374151")
                .encode(
                    x=alt.X("Count:Q", axis=alt.Axis(labelFontSize=9, title=None)),
                    y=alt.Y("Country:N", sort="-x",
                            axis=alt.Axis(labelFontSize=10, title=None)),
                    tooltip=["Country", "Count"],
                )
                .properties(height=max(60, min(len(ctry_counts), 10) * 28), background="white")
                .configure_view(strokeWidth=0, fill="white")
                .configure_axis(grid=False, labelColor="#111111", titleColor="#111111")
            )
            st.altair_chart(ctry_chart, use_container_width=True)
        else:
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_muted"]};">Country data not available for this pipeline.</div>',
                unsafe_allow_html=True,
            )


# ─────────────────────────────────────────────────────────────────────────────

def _render_detail(company_id: int) -> None:
    force_scroll_top()
    row = load_company(company_id)
    if not row:
        st.error("Company not found.")
        return

    name   = row["name"]
    tier   = row.get("tier")
    score  = float(row.get("total_score") or 0)
    status = row.get("status") or "sourced"

    origin = st.session_state.get("nav_origin")
    if origin == "portfolio":
        crumb_root, crumb_middle = "PORTFOLIO", "Related Pipeline"
        back_label, back_page = "← BACK TO PORTFOLIO", "portfolio"
    elif origin == "pipeline":
        crumb_root, crumb_middle = "PIPELINE", None
        back_label, back_page = "← BACK TO PIPELINE", "pipeline"
    elif origin == "press":
        crumb_root, crumb_middle = "PRESS", None
        back_label, back_page = "← BACK TO PRESS", "press"
    else:
        crumb_root, crumb_middle = "DEAL FLOW", None
        back_label, back_page = "← BACK TO DEAL FLOW", "overview"

    def _go_back() -> None:
        st.session_state["selected_company_id"] = None
        st.session_state["nav_origin"] = None
        st.session_state["page"] = back_page
        st.rerun()

    crumb_cols = st.columns([3, 12])
    with crumb_cols[0]:
        st.markdown('<div class="ua-crumb-link"></div>', unsafe_allow_html=True)
        if st.button(crumb_root, key="crumb_origin", use_container_width=True):
            _go_back()
    with crumb_cols[1]:
        if crumb_middle:
            suffix_html = f'&rsaquo; {crumb_middle} &rsaquo; {name.upper()}'
        else:
            suffix_html = f'&rsaquo; {name.upper()}'
        st.markdown(
            f'<div style="font-size:10px;color:{COLORS["text_muted"]};letter-spacing:0.08em;'
            f'padding-top:12px;text-transform:uppercase;">{suffix_html}</div>',
            unsafe_allow_html=True,
        )

    if st.button(back_label, key="dealflow_back"):
        _go_back()

    hdr, score_col, status_col = st.columns([4, 1, 1])
    with hdr:
        st.markdown(
            f'<h1 style="font-size:20px;margin:0 0 8px;">{name.upper()}</h1>',
            unsafe_allow_html=True,
        )
        badges = tier_badge(tier, row.get("tier_label")) if tier else ""
        if row.get("primary_category"):
            badges += "&nbsp;" + category_badge(row["primary_category"])
        buzz = compute_buzz_score(company_id, days=30)
        badges += "&nbsp;" + _buzz_badge_html(buzz)
        if row.get("website"):
            badges += (
                f'&nbsp;<a href="{row["website"]}" target="_blank" '
                f'style="font-size:10px;color:{COLORS["text_secondary"]};font-weight:600;">'
                f'{row["website"][:45]}{"…" if len(row["website"])>45 else ""}</a>'
            )
        st.markdown(badges, unsafe_allow_html=True)

    with score_col:
        st.markdown(stat_html("Score", f"{score:.2f}", "/ 3.00"), unsafe_allow_html=True)

    with status_col:
        cur_idx   = STATUS_STAGES.index(status) if status in STATUS_STAGES else 0
        new_status = st.selectbox(
            "Status",
            STATUS_STAGES,
            index=cur_idx,
            key=f"det_status_{company_id}",
            format_func=lambda s: STATUS_LABELS.get(s, s),
        )
        if new_status != status:
            _write(
                "UPDATE companies SET status=?, status_updated_at=datetime('now') WHERE id=?",
                (new_status, company_id),
            )
            st.cache_data.clear()
            st.rerun()

    st.markdown(
        f'<hr style="border:none;border-top:1px solid {COLORS["border_light"]};margin:16px 0;">',
        unsafe_allow_html=True,
    )

    left, right = st.columns([3, 2])

    with left:
        section_header("OVERVIEW", margin_top=0)
        overview_fields = [
            ("Description",  row.get("description"),
             row.get("description_confidence")),
            ("HQ Country",   row.get("hq_country"),
             row.get("hq_country_confidence")),
            ("Founded",      row.get("founded_year"),
             row.get("founded_year_confidence")),
            ("Funding",      row.get("funding_summary"),
             row.get("funding_confidence")),
        ]
        any_overview = any(v for _, v, _ in overview_fields)
        if any_overview:
            for label, value, conf in overview_fields:
                if value in (None, ""):
                    continue
                st.markdown(
                    f'<div style="font-size:12px;line-height:1.6;'
                    f'margin-bottom:10px;color:{COLORS["text"]};">'
                    f'{confidence_dot(conf, label=label)}'
                    f'<span style="font-weight:700;color:{COLORS["text_secondary"]};'
                    f'letter-spacing:0.04em;text-transform:uppercase;font-size:10px;">'
                    f'{label}:</span> '
                    f'<span style="margin-left:6px;">{value}</span>'
                    f'{confidence_tag(conf)}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:8px 0;">'
                'No AI-extracted overview yet.</div>',
                unsafe_allow_html=True,
            )

        section_header("SCORE BREAKDOWN")
        breakdown = json.loads(row.get("score_breakdown") or "{}")
        if breakdown:
            bd_df = pd.DataFrame([
                {
                    "Dimension": DIM_LABELS.get(d, d),
                    "Score":     v["score"],
                    "Reason":    v.get("reason", ""),
                }
                for d, v in breakdown.items()
            ])
            chart = (
                alt.Chart(bd_df)
                .mark_bar(color="#374151")
                .encode(
                    x=alt.X("Score:Q", scale=alt.Scale(domain=[0, 3]),
                            axis=alt.Axis(tickCount=4, labelFontSize=10)),
                    y=alt.Y("Dimension:N", sort=None,
                            axis=alt.Axis(labelFontSize=11, labelFont="Inter")),
                    tooltip=["Dimension", "Score", "Reason"],
                )
                .properties(height=185, background="white")
                .configure_view(strokeWidth=0, fill="white")
                .configure_axis(grid=False, labelColor="#111111", titleColor="#111111")
            )
            st.altair_chart(chart, use_container_width=True)
            for d, v in breakdown.items():
                st.markdown(
                    f'<div style="font-size:10px;color:{COLORS["text_secondary"]};margin-bottom:4px;">'
                    f'<b style="color:{COLORS["text"]};font-weight:700;">{DIM_LABELS.get(d, d)}</b>'
                    f' {v["score"]}/3 — {v.get("reason", "")}</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:12px 0;">'
                'Not yet scored.</div>',
                unsafe_allow_html=True,
            )

        section_header("THESIS FIT RADAR")
        rc = radar_chart(breakdown)
        if rc:
            st.altair_chart(rc, use_container_width=False)
        else:
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:4px 0;">'
                'Score breakdown not available for radar chart.</div>',
                unsafe_allow_html=True,
            )

        section_header("SCORE TRAJECTORY")
        history = load_score_history(company_id)
        if len(history) > 1:
            hist_df = pd.DataFrame(history)
            hist_df["scored_at"] = pd.to_datetime(hist_df["scored_at"])
            _base = alt.Chart(hist_df).encode(
                x=alt.X("scored_at:T", axis=alt.Axis(labelFontSize=9, format="%b %d", title=None)),
                y=alt.Y("total_score:Q", scale=alt.Scale(domain=[0, 3]),
                        axis=alt.Axis(tickCount=4, labelFontSize=9, title=None)),
                tooltip=[
                    alt.Tooltip("scored_at:T", title="Date", format="%b %d %Y"),
                    alt.Tooltip("total_score:Q", title="Score", format=".2f"),
                ],
            )
            traj_chart = (
                alt.layer(
                    _base.mark_line(color="#111111", strokeWidth=2),
                    _base.mark_point(color="#111111", size=40, filled=True),
                )
                .properties(height=120, background="white")
                .configure_view(strokeWidth=0, fill="white")
                .configure_axis(grid=False, labelColor="#111111", titleColor="#111111")
            )
            st.altair_chart(traj_chart, use_container_width=True)
        elif len(history) == 1:
            score_val = history[0]["total_score"]
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_secondary"]};padding:8px 0;">'
                f'One score recorded: {score_val:.2f}. Trend visible after next scorer run.</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:8px 0;">'
                'No score history yet — history is recorded on each future scorer run.</div>',
                unsafe_allow_html=True,
            )

        section_header("DOSSIER SUMMARY")
        summary = row.get("dossier_summary")
        if summary:
            st.markdown(summary)
        else:
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:8px 0;">'
                'No dossier yet. Use Generate Dossier to run the analysis.</div>',
                unsafe_allow_html=True,
            )

        section_header("INVESTMENT MEMO")
        _memo_val = row.get("memo") or ""
        _is_html_memo = _memo_val.endswith(".html")
        if _is_html_memo:
            _memo_path = Path(_memo_val)
            if _memo_path.exists():
                _memo_html = _memo_path.read_text(encoding="utf-8")
                _memo_at   = (row.get("memo_at") or "")[:10]
                _dl_col, _regen_col = st.columns(2)
                with _dl_col:
                    st.download_button(
                        "DOWNLOAD MEMO (HTML)",
                        data=_memo_html,
                        file_name=f"{slug(name)}_memo.html",
                        mime="text/html",
                        key=f"dl_html_memo_{company_id}",
                        use_container_width=True,
                    )
                with _regen_col:
                    st.markdown(
                        f'<div style="font-size:10px;color:#16a34a;font-weight:700;'
                        f'letter-spacing:0.06em;padding:12px 0;">GENERATED{(" · " + _memo_at) if _memo_at else ""}</div>',
                        unsafe_allow_html=True,
                    )
                with st.expander("PREVIEW MEMO", expanded=False):
                    import streamlit.components.v1 as _components
                    _components.html(_memo_html, height=640, scrolling=True)
            else:
                st.markdown(
                    '<div style="font-size:12px;color:#d97706;padding:4px 0;">'
                    'Memo file missing — regenerate from Actions.</div>',
                    unsafe_allow_html=True,
                )
        elif _memo_val:
            with st.expander("View memo (legacy markdown)", expanded=False):
                st.markdown(_memo_val)
        else:
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_muted"]};font-style:italic;padding:4px 0;">'
                'No memo generated yet — click Generate Memo in Actions.</div>',
                unsafe_allow_html=True,
            )

    with right:
        section_header("SIMILAR COMPANIES", margin_top=0)
        _conn = sqlite3.connect(DB_PATH)
        _conn.row_factory = sqlite3.Row
        peers = _landscape.similar_companies(company_id, top_n=5, conn=_conn)
        _conn.close()
        if peers:
            peer_rows = []
            for p in peers:
                score_str = f'{float(p["total_score"]):.2f}' if p["total_score"] is not None else "—"
                tier_str  = f'T{p["tier"]}' if p.get("tier") else "—"
                desc      = (p.get("description") or "")[:80]
                if len(p.get("description") or "") > 80:
                    desc += "…"
                peer_rows.append({
                    "Company":     p["name"],
                    "Score":       score_str,
                    "Tier":        tier_str,
                    "Description": desc,
                    "_id":         p["id"],
                })
            peer_df = pd.DataFrame(peer_rows)
            st.markdown(
                f'<div style="font-size:10px;color:{COLORS["text_secondary"]};margin-bottom:8px;">'
                'Click a row to navigate to that company.</div>',
                unsafe_allow_html=True,
            )
            peer_event = st.dataframe(
                peer_df.drop(columns=["_id"]),
                hide_index=True,
                use_container_width=True,
                height=min(len(peer_rows) * 35 + 38, 215),
                on_select="rerun",
                selection_mode="single-row",
                key=f"landscape_{company_id}",
                column_config={
                    "Score":       st.column_config.TextColumn("Score", width="small"),
                    "Tier":        st.column_config.TextColumn("Tier",  width="small"),
                    "Description": st.column_config.TextColumn("Description"),
                },
            )
            if peer_event.selection.rows:
                nav_id = int(peer_df.iloc[peer_event.selection.rows[0]]["_id"])
                st.session_state.selected_company_id = nav_id
                st.session_state.nav_origin = None
        else:
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_muted"]};">No peers in same category yet.</div>',
                unsafe_allow_html=True,
            )

        section_header("SOURCE INTELLIGENCE")
        source_urls = json.loads(row.get("source_urls") or "[]")
        country_val = row.get("hq_country") or "—"
        country_conf = row.get("hq_country_confidence")
        country_dot = (
            confidence_dot(country_conf, label="HQ Country")
            if row.get("hq_country") else ""
        )
        st.markdown(
            f'<div style="font-size:12px;line-height:1.8;">'
            f'<b>Source:</b> {row.get("source", "—")}<br>'
            f'<b>Mentions:</b> {row.get("mention_count") or "—"}<br>'
            f'<b>Cross-validated:</b> {"Yes" if row.get("cross_validated") else "No"}<br>'
            f'<b>Country:</b> {country_dot}{country_val}'
            f'</div>',
            unsafe_allow_html=True,
        )
        if source_urls:
            st.markdown(
                '<div style="font-size:10px;font-weight:700;letter-spacing:0.06em;'
                f'text-transform:uppercase;color:{COLORS["text_secondary"]};margin-top:12px;margin-bottom:4px;">'
                'Article URLs</div>',
                unsafe_allow_html=True,
            )
            for url in source_urls[:4]:
                st.markdown(
                    f'<div style="font-size:10px;margin-bottom:4px;">'
                    f'<a href="{url}" target="_blank" style="color:#374151;">'
                    f'{url[:55]}{"…" if len(url) > 55 else ""}</a></div>',
                    unsafe_allow_html=True,
                )
            if len(source_urls) > 4:
                st.markdown(
                    f'<div style="font-size:10px;color:{COLORS["text_muted"]};">+{len(source_urls)-4} more</div>',
                    unsafe_allow_html=True,
                )

        section_header("WHY THIS COMPANY")
        _traction   = json.loads(row.get("traction_signals") or "[]")
        _src_urls   = json.loads(row.get("source_urls") or "[]")
        _cross      = row.get("cross_validated")
        _mentions   = row.get("mention_count") or 0
        _source_nm  = row.get("source") or "unknown"
        _why_items  = []
        if _cross:
            _why_items.append(("✓ Cross-validated across multiple sources", "#16a34a"))
        if int(_mentions) > 1:
            _why_items.append((f"✓ Mentioned {_mentions}× in defense-tech coverage", "#16a34a"))
        for t in _traction[:2]:
            _why_items.append((f"✓ {t}", "#16a34a"))
        if _source_nm == "prozorro":
            _why_items.append(("✓ Identified via ProZorro defense procurement", "#374151"))
        elif _source_nm == "article":
            _why_items.append(("✓ Identified via defense-tech article coverage", "#374151"))
        elif _source_nm == "seed":
            _why_items.append(("✓ Seed list — curated sourcing input", "#374151"))
        if len(_src_urls) > 1:
            _why_items.append((f"✓ {len(_src_urls)} source URLs collected", "#374151"))
        if row.get("primary_category"):
            _port_names = [
                co for co, tags in _PORTFOLIO_TAGS.items()
                if any(row["primary_category"].lower() in t.lower() for t in tags)
            ]
            if _port_names:
                _port_str = ", ".join(_port_names[:2])
                _why_items.append((f"✓ Same category as portfolio: {_port_str}", "#d97706"))
        if _why_items:
            for _item_text, _item_color in _why_items:
                st.markdown(
                    f'<div style="font-size:12px;color:{_item_color};margin-bottom:4px;'
                    f'line-height:1.5;">{_item_text}</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_muted"]};">Insufficient data for analysis.</div>',
                unsafe_allow_html=True,
            )

        section_header("NOTES")
        current_notes = row.get("user_notes") or ""
        new_notes = st.text_area(
            "Notes",
            value=current_notes,
            height=90,
            key=f"notes_{company_id}",
            label_visibility="collapsed",
            placeholder="Add analyst notes here...",
        )
        if st.button("SAVE NOTES", key=f"save_notes_{company_id}"):
            _write("UPDATE companies SET user_notes=? WHERE id=?", (new_notes, company_id))
            st.cache_data.clear()
            st.success("Saved.")

        section_header("ACTIONS")
        a1, a2 = st.columns(2)
        with a1:
            has_dossier = bool(row.get("dossier_at"))
            if not has_dossier:
                if st.button("GENERATE DOSSIER", key=f"gen_dos_{company_id}"):
                    with st.spinner("Running dossier pipeline — 1-2 min…"):
                        import dossier as _dossier
                        import classify
                        _dossier.run(company_names=[name])
                        classify.run()
                    st.cache_data.clear()
                    st.rerun()
            else:
                st.markdown(
                    '<div style="font-size:10px;color:#16a34a;font-weight:700;'
                    'padding:8px 0;letter-spacing:0.06em;">DOSSIER COMPLETE</div>',
                    unsafe_allow_html=True,
                )

        with a2:
            _memo_val     = row.get("memo") or ""
            _has_html     = _memo_val.endswith(".html")
            _has_any_memo = bool(_memo_val)
            if has_dossier:
                if not _has_html:
                    _btn_lbl = "GENERATE MEMO" if not _has_any_memo else "GENERATE MEMO (HTML)"
                    if st.button(_btn_lbl, key=f"gen_memo_{company_id}"):
                        with st.spinner("Generating HTML investment memo — 30-60s…"):
                            import memo_generator
                            _mc = sqlite3.connect(DB_PATH)
                            _mc.row_factory = sqlite3.Row
                            try:
                                memo_generator.generate_html_memo(company_id, _mc)
                            finally:
                                _mc.close()
                        st.cache_data.clear()
                        st.rerun()
                else:
                    if st.button("REGENERATE MEMO", key=f"regen_memo_{company_id}"):
                        with st.spinner("Regenerating HTML investment memo…"):
                            import memo_generator
                            _mc = sqlite3.connect(DB_PATH)
                            _mc.row_factory = sqlite3.Row
                            try:
                                memo_generator.generate_html_memo(company_id, _mc, force_refresh=True)
                            finally:
                                _mc.close()
                        st.cache_data.clear()
                        st.rerun()

    # ── Full-width founders + contacts ────────────────────────────────────────
    st.markdown(
        f'<hr style="border:none;border-top:1px solid {COLORS["border_light"]};margin:32px 0 0;">',
        unsafe_allow_html=True,
    )
    _render_founders(company_id, row.get("founders_confidence"))
    _render_contacts(company_id)

    # ── Full-width recent press ───────────────────────────────────────────────
    st.markdown(
        f'<hr style="border:none;border-top:1px solid {COLORS["border_light"]};margin:32px 0 0;">',
        unsafe_allow_html=True,
    )
    _render_press_mentions(company_id)

    # ── Full-width competitive landscape ──────────────────────────────────────
    st.markdown(
        f'<hr style="border:none;border-top:1px solid {COLORS["border_light"]};margin:32px 0 0;">',
        unsafe_allow_html=True,
    )
    section_header("COMPETITIVE LANDSCAPE", margin_top=16)
    _render_competitive_landscape(row)


def _render_founders(company_id: int, agg_confidence: float | None) -> None:
    rows = _read(
        "SELECT name, role, linkedin_url, twitter_handle, bio, background, "
        "confidence FROM founders WHERE company_id = ? ORDER BY confidence DESC",
        (company_id,),
    )
    header = "FOUNDERS"
    if agg_confidence is not None:
        # Aggregate dot lives in the section header, signaling the LLM's overall
        # confidence in its founder extraction (vs. per-card per-person score).
        section_header(header, margin_top=16)
    else:
        section_header(header, margin_top=16)

    if not rows:
        st.markdown(
            f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:8px 0;">'
            'No founder data available — may not have been mentioned in source material.'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    # Two-column grid of founder cards.
    for i in range(0, len(rows), 2):
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(founder_card(dict(rows[i])), unsafe_allow_html=True)
        if i + 1 < len(rows):
            with c2:
                st.markdown(founder_card(dict(rows[i + 1])), unsafe_allow_html=True)


def _render_contacts(company_id: int) -> None:
    rows = _read(
        "SELECT type, value, confidence FROM contacts "
        "WHERE company_id = ? ORDER BY confidence DESC",
        (company_id,),
    )
    section_header("CONTACTS", margin_top=24)
    if not rows:
        st.markdown(
            f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:8px 0;">'
            'No contact info found — may not have been listed publicly.</div>',
            unsafe_allow_html=True,
        )
        return
    pieces = [contact_row(dict(r)) for r in rows]
    st.markdown(
        '<div style="font-family:Inter,sans-serif;">' + "".join(pieces) + "</div>",
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────

def _render_press_mentions(company_id: int, limit: int = 10) -> None:
    """Most recent articles mentioning this company. Limited to `limit` rows."""
    import html as _html
    section_header("RECENT PRESS", margin_top=16)

    mentions = load_company_press_mentions(company_id, limit=limit)
    if not mentions:
        st.markdown(
            f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:8px 0;">'
            'No press coverage yet.</div>',
            unsafe_allow_html=True,
        )
        return

    pieces: list[str] = []
    for m in mentions:
        title = (m.get("title") or "(untitled)").strip()
        url = m.get("url") or ""
        pub = m.get("publication") or "—"
        date_str = (m.get("published_at") or m.get("processed_at") or "").split(" ")[0]
        rel = m.get("relevance_score")
        ctx = (m.get("context") or "").strip()
        tags = m.get("sector_tags_list") or []

        rel_badge = ""
        if rel is not None:
            if rel >= 0.7:
                bg = COLORS["gap_covered"]
            elif rel >= 0.4:
                bg = COLORS["gap_opportunity"]
            else:
                bg = COLORS["text_muted"]
            rel_badge = (
                f'<span style="background:{bg};color:#ffffff;padding:2px 8px;'
                f'font-size:9px;font-weight:700;letter-spacing:0.06em;'
                f'margin-left:8px;font-family:Inter,sans-serif;">'
                f'REL {rel:.2f}</span>'
            )

        tags_html = "".join(
            f'<span style="background:transparent;color:{COLORS["text_secondary"]};'
            f'border:1px solid {COLORS["border_mid"]};padding:1px 6px;font-size:9px;'
            f'font-weight:600;letter-spacing:0.04em;margin-right:6px;'
            f'display:inline-block;">{_html.escape(t)}</span>'
            for t in tags
        )
        ctx_html = (
            f'<div style="font-size:11px;color:{COLORS["text_muted"]};font-style:italic;'
            f'line-height:1.5;margin-top:6px;">"{_html.escape(ctx)}"</div>'
            if ctx else ""
        )

        title_anchor = (
            f'<a href="{_html.escape(url)}" target="_blank" '
            f'style="font-size:12px;font-weight:800;color:{COLORS["text"]};'
            f'text-decoration:none;line-height:1.3;">{_html.escape(title)}</a>'
            if url else
            f'<span style="font-size:12px;font-weight:800;color:{COLORS["text"]};'
            f'line-height:1.3;">{_html.escape(title)}</span>'
        )

        pieces.append(
            f'<div style="background:{COLORS["card_bg"]};'
            f'border:1px solid {COLORS["border_mid"]};padding:12px;margin-bottom:10px;">'
            f'<div>{title_anchor}</div>'
            f'<div style="font-size:10px;color:{COLORS["text_muted"]};letter-spacing:0.04em;'
            f'text-transform:uppercase;font-weight:700;margin-top:4px;">'
            f'{_html.escape(pub)}'
            f'{(" &nbsp;·&nbsp; " + _html.escape(date_str)) if date_str else ""}'
            f'{rel_badge}</div>'
            f'{ctx_html}'
            f'{(f"<div style=margin-top:8px;>{tags_html}</div>") if tags_html else ""}'
            f'</div>'
        )

    st.markdown("".join(pieces), unsafe_allow_html=True)


def _render_competitive_landscape(row: dict) -> None:
    company_id = row["id"]
    category   = row.get("primary_category")

    if not category:
        st.markdown(
            f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:8px 0 16px;">'
            'Category not yet assigned — competitive landscape unavailable.</div>',
            unsafe_allow_html=True,
        )
        return

    # ── Fetch data ────────────────────────────────────────────────────────────
    cat_stats = _read(
        """SELECT COUNT(*) AS total, AVG(total_score) AS avg_score
             FROM companies
            WHERE primary_category = ?
              AND (portfolio_company IS NULL OR portfolio_company = 0)
              AND (status IS NULL OR status != 'duplicate')""",
        (category,),
    )[0]

    scored_rows = _read(
        """SELECT id, total_score
             FROM companies
            WHERE primary_category = ?
              AND (portfolio_company IS NULL OR portfolio_company = 0)
              AND (status IS NULL OR status != 'duplicate')
              AND total_score IS NOT NULL
            ORDER BY total_score DESC""",
        (category,),
    )
    rank = next((i + 1 for i, r in enumerate(scored_rows) if r["id"] == company_id), None)

    portfolio_raw = _read(
        """SELECT id, name, description, source, website
             FROM companies
            WHERE portfolio_company = 1
              AND primary_category = ?
            ORDER BY name""",
        (category,),
    )

    peers_raw = _read(
        """SELECT id, name, total_score, tier, hq_country, description, source
             FROM companies
            WHERE primary_category = ?
              AND id != ?
              AND (portfolio_company IS NULL OR portfolio_company = 0)
              AND (status IS NULL OR status != 'duplicate')
            ORDER BY total_score DESC
            LIMIT 8""",
        (category, company_id),
    )

    # ── Metrics row ───────────────────────────────────────────────────────────
    total_in_cat  = int(cat_stats["total"] or 0)
    avg_score     = float(cat_stats["avg_score"] or 0)
    current_score = row.get("total_score")
    n_scored      = len(scored_rows)
    n_portfolio   = len(portfolio_raw)

    if rank:
        rank_note = f"Rank {rank} of {n_scored} scored"
    else:
        rank_note = "Not yet scored"

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.markdown(stat_html("In Category", total_in_cat, category[:24]), unsafe_allow_html=True)
    with m2:
        st.markdown(stat_html("Avg Score", f"{avg_score:.2f}", "category average"), unsafe_allow_html=True)
    with m3:
        score_display = f"{float(current_score):.2f}" if current_score is not None else "—"
        st.markdown(stat_html("This Company", score_display, rank_note), unsafe_allow_html=True)
    with m4:
        st.markdown(stat_html("Portfolio", n_portfolio, "in this category"), unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── portfolio companies ──────────────────────────────────────────────────
    if portfolio_raw:
        st.markdown(
            '<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
            'text-transform:uppercase;color:#059669;margin-bottom:8px;">'
            'PORTFOLIO — SAME CATEGORY</div>',
            unsafe_allow_html=True,
        )
        for p in portfolio_raw:
            desc = (p.get("description") or "—")[:140]
            if len(p.get("description") or "") > 140:
                desc += "…"
            site = p.get("website") or ""
            site_html = (
                f'<a href="{site}" target="_blank" style="font-size:10px;color:#059669;">'
                f'{site[:55]}</a>'
            ) if site else ""
            st.markdown(
                f'<div style="border-left:4px solid {COLORS["gap_covered"]};padding:12px 16px;'
                f'background:#f0fdf4;margin-bottom:8px;">'
                f'<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">'
                f'<span style="font-size:12px;font-weight:800;color:{COLORS["text"]};'
                f'letter-spacing:0.03em;">{p["name"].upper()}</span>'
                f'<span style="background:{COLORS["gap_covered"]};color:#ffffff;padding:4px 8px;'
                f'font-size:10px;font-weight:700;letter-spacing:0.07em;">PORTFOLIO</span>'
                f'<span style="font-size:10px;color:{COLORS["text_secondary"]};">{p.get("source", "—")}</span>'
                f'</div>'
                f'<div style="font-size:10px;color:#374151;margin-top:4px;line-height:1.5;">{desc}</div>'
                f'<div style="margin-top:4px;">{site_html}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        st.markdown("<br>", unsafe_allow_html=True)

    # ── Pipeline competitors ──────────────────────────────────────────────────
    if not peers_raw:
        st.markdown(
            f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:4px 0 16px;">'
            'No other pipeline companies in this category yet.</div>',
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        f'<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:{COLORS["text_secondary"]};margin-bottom:8px;">'
        f'PIPELINE — TOP {len(peers_raw)} IN CATEGORY</div>',
        unsafe_allow_html=True,
    )

    _TIER_BG   = {1: COLORS["tier1"], 2: COLORS["tier2"], 3: COLORS["tier3"], 4: COLORS["tier4"]}
    _TIER_TEXT = {1: "#ffffff",       2: "#ffffff",       3: "#ffffff",       4: COLORS["text"]}

    hdr = st.columns([3, 1, 1, 1, 5])
    for col, lbl in zip(hdr, ["Company", "Score", "Tier", "Country", "Description"]):
        with col:
            st.markdown(
                f'<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
                f'text-transform:uppercase;color:{COLORS["text_secondary"]};padding-bottom:4px;">{lbl}</div>',
                unsafe_allow_html=True,
            )

    for p in peers_raw:
        pid       = int(p["id"])
        score_str = f'{float(p["total_score"]):.2f}' if p.get("total_score") is not None else "—"
        tv        = int(p["tier"]) if p.get("tier") is not None else 4
        desc      = (p.get("description") or "—")[:110]
        if len(p.get("description") or "") > 110:
            desc += "…"

        row_cols = st.columns([3, 1, 1, 1, 5])
        with row_cols[0]:
            if st.button(p["name"], key=f"cl_peer_{company_id}_{pid}", use_container_width=True):
                st.session_state.selected_company_id = pid
                st.session_state.page = "detail"
                st.session_state.nav_origin = None
                st.rerun()
        with row_cols[1]:
            st.markdown(
                f'<div style="padding:8px 0;font-size:12px;font-weight:700;">{score_str}</div>',
                unsafe_allow_html=True,
            )
        with row_cols[2]:
            st.markdown(
                f'<div style="padding:8px 0;">'
                f'<span style="background:{_TIER_BG[tv]};color:{_TIER_TEXT[tv]};'
                f'padding:4px 8px;font-size:10px;font-weight:700;letter-spacing:0.05em;">'
                f'T{tv}</span></div>',
                unsafe_allow_html=True,
            )
        with row_cols[3]:
            st.markdown(
                f'<div style="padding:8px 0;font-size:10px;color:{COLORS["text_secondary"]};">'
                f'{p.get("hq_country") or "—"}</div>',
                unsafe_allow_html=True,
            )
        with row_cols[4]:
            st.markdown(
                f'<div style="padding:8px 0;font-size:10px;color:#374151;line-height:1.4;">'
                f'{desc}</div>',
                unsafe_allow_html=True,
            )
