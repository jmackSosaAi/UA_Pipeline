import datetime

import pandas as pd
import streamlit as st

from ui.components import outreach_badge, page_title, stat_html
from ui.data import (
    OUTREACH_STATUSES,
    OUTREACH_STATUS_COLORS,
    STATUS_LABELS,
    STATUS_STAGES,
    _write,
    load_companies,
)
from ui.theme import COLORS

_DEFAULT_OUTREACH = "Not Started"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _badge(status: str) -> str:
    return outreach_badge(status or _DEFAULT_OUTREACH)


def _parse_date(val) -> datetime.date | None:
    if not val:
        return None
    try:
        return datetime.date.fromisoformat(str(val)[:10])
    except (ValueError, TypeError):
        return None


def _save_outreach(cid: int, r: pd.Series, new_vals: dict) -> None:
    today = datetime.date.today().isoformat()
    old_status = r.get("outreach_status") or _DEFAULT_OUTREACH
    new_status  = new_vals["status"]

    first_contact = r.get("outreach_date_first_contact") or None
    last_contact  = r.get("outreach_date_last_contact")  or None

    if new_status != _DEFAULT_OUTREACH:
        if old_status == _DEFAULT_OUTREACH and not first_contact:
            first_contact = today
        if new_status != old_status:
            last_contact = today

    nxt_date = new_vals["next_action_date"]
    _write(
        """UPDATE companies SET
               outreach_status             = ?,
               outreach_owner              = ?,
               outreach_notes              = ?,
               outreach_next_action        = ?,
               outreach_next_action_date   = ?,
               outreach_date_first_contact = ?,
               outreach_date_last_contact  = ?
           WHERE id = ?""",
        (
            new_status,
            new_vals["owner"]       or None,
            new_vals["notes"]       or None,
            new_vals["next_action"] or None,
            nxt_date.isoformat() if isinstance(nxt_date, datetime.date) else None,
            first_contact,
            last_contact,
            cid,
        ),
    )


# ── Outreach panel (per-company expander) ─────────────────────────────────────

def _render_outreach_panel(r: pd.Series) -> None:
    cid            = int(r["id"])
    current_status = r.get("outreach_status") or _DEFAULT_OUTREACH
    score_str      = f'{float(r["total_score"]):.2f}' if pd.notna(r.get("total_score")) else "—"
    tier_str       = f'T{int(r["tier"])}' if pd.notna(r.get("tier")) else "—"
    cat_str        = r.get("primary_category") or "—"

    # Context header
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;gap:16px;align-items:center;'
        f'padding:8px 0 12px;border-bottom:1px solid {COLORS["border_light"]};margin-bottom:12px;">'
        f'<span style="font-size:12px;font-weight:700;">{r["name"]}</span>'
        f'<span style="font-size:12px;color:{COLORS["text_secondary"]};">Score: <b>{score_str}</b></span>'
        f'<span style="font-size:12px;color:{COLORS["text_secondary"]};">Tier: <b>{tier_str}</b></span>'
        f'<span style="font-size:12px;color:{COLORS["text_secondary"]};">{cat_str}</span>'
        f'{_badge(current_status)}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Read-only contact dates
    first_c = r.get("outreach_date_first_contact") or "—"
    last_c  = r.get("outreach_date_last_contact")  or "—"
    st.markdown(
        f'<div style="font-size:10px;color:{COLORS["text_muted"]};margin-bottom:16px;">'
        f'First contact: <b style="color:{COLORS["text_secondary"]};">{first_c}</b>'
        f'&nbsp;&nbsp;|&nbsp;&nbsp;'
        f'Last contact: <b style="color:{COLORS["text_secondary"]};">{last_c}</b>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Editable form
    with st.form(key=f"outreach_{cid}"):
        c1, c2 = st.columns(2)
        with c1:
            new_status = st.selectbox(
                "Outreach Status",
                OUTREACH_STATUSES,
                index=OUTREACH_STATUSES.index(current_status)
                      if current_status in OUTREACH_STATUSES else 0,
            )
            owner = st.text_input(
                "Owner",
                value=r.get("outreach_owner") or "",
                placeholder="e.g. Jackson",
            )
        with c2:
            next_action = st.text_input(
                "Next Action",
                value=r.get("outreach_next_action") or "",
                placeholder="e.g. Follow up on intro",
            )
            next_action_date = st.date_input(
                "Next Action Date",
                value=_parse_date(r.get("outreach_next_action_date")),
            )
        notes = st.text_area(
            "Notes",
            value=r.get("outreach_notes") or "",
            height=80,
            placeholder="Freeform notes...",
        )
        if st.form_submit_button("Save Changes", type="primary"):
            _save_outreach(cid, r, {
                "status":           new_status,
                "owner":            owner,
                "notes":            notes,
                "next_action":      next_action,
                "next_action_date": next_action_date,
            })
            st.cache_data.clear()
            st.toast("Saved")
            st.rerun()


# ── Main render ───────────────────────────────────────────────────────────────

def render(conn=None) -> None:
    df = load_companies()

    page_title(
        "PIPELINE",
        "Track deal progression and outreach. Update status to move companies through stages.",
    )

    # ── Deal stage funnel ─────────────────────────────────────────────────────
    stage_counts: dict[str, int] = {s: 0 for s in STATUS_STAGES}
    if not df.empty:
        for s, cnt in df["status"].value_counts().items():
            if s in stage_counts:
                stage_counts[s] = int(cnt)

    cells = ""
    for stage in STATUS_STAGES:
        cells += (
            f'<div style="flex:1;text-align:center;border-right:1px solid {COLORS["border_light"]};'
            f'padding:12px 8px;min-width:0;">'
            f'<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
            f'text-transform:uppercase;color:{COLORS["text_secondary"]};white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis;">{STATUS_LABELS[stage]}</div>'
            f'<div style="font-size:26px;font-weight:800;color:{COLORS["text"]};margin-top:4px;">'
            f'{stage_counts[stage]}</div></div>'
        )
    st.markdown(
        f'<div style="display:flex;background:{COLORS["card_bg"]};'
        f'border:1px solid {COLORS["card_border"]};margin-bottom:20px;">'
        f'{cells}</div>',
        unsafe_allow_html=True,
    )

    # ── Outreach tracker header ───────────────────────────────────────────────
    st.markdown(
        f'<div style="font-size:12px;font-weight:800;letter-spacing:0.09em;'
        f'text-transform:uppercase;border-bottom:2px solid {COLORS["text"]};'
        f'padding-bottom:4px;margin:4px 0 12px;">OUTREACH TRACKER</div>',
        unsafe_allow_html=True,
    )

    # Count per outreach status
    outreach_counts: dict[str, int] = {s: 0 for s in OUTREACH_STATUSES}
    if not df.empty:
        for s, cnt in df["outreach_status"].fillna(_DEFAULT_OUTREACH).value_counts().items():
            if s in outreach_counts:
                outreach_counts[s] = int(cnt)

    # Colored summary bar
    oc_cells = ""
    for s in OUTREACH_STATUSES:
        color = OUTREACH_STATUS_COLORS.get(s, COLORS["text_secondary"])
        oc_cells += (
            f'<div style="flex:1;text-align:center;border-right:1px solid {COLORS["border_light"]};'
            f'padding:8px 4px;min-width:0;">'
            f'<div style="font-size:10px;font-weight:700;letter-spacing:0.05em;'
            f'text-transform:uppercase;color:{color};white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis;">{s}</div>'
            f'<div style="font-size:20px;font-weight:800;color:{COLORS["text"]};margin-top:4px;">'
            f'{outreach_counts.get(s, 0)}</div></div>'
        )
    st.markdown(
        f'<div style="display:flex;background:{COLORS["card_bg_light"]};'
        f'border:1px solid {COLORS["border_mid"]};margin-bottom:12px;">'
        f'{oc_cells}</div>',
        unsafe_allow_html=True,
    )

    # Clickable filter
    filter_options = ["All"] + OUTREACH_STATUSES
    selected_filter = st.radio(
        "Filter by outreach status",
        filter_options,
        horizontal=True,
        key="outreach_filter_radio",
        label_visibility="collapsed",
        format_func=lambda s: s if s == "All" else f"{s} ({outreach_counts.get(s, 0)})",
    )
    active_filter = None if selected_filter == "All" else selected_filter

    if df.empty:
        st.info("No companies found.")
        return

    # ── Stage buckets ─────────────────────────────────────────────────────────
    for stage in STATUS_STAGES:
        stage_df = df[df["status"] == stage]

        if active_filter:
            stage_df = stage_df[
                stage_df["outreach_status"].fillna(_DEFAULT_OUTREACH) == active_filter
            ]

        if stage_df.empty:
            continue

        st.markdown(
            f'<div style="font-size:12px;font-weight:800;letter-spacing:0.09em;'
            f'text-transform:uppercase;border-bottom:2px solid {COLORS["text"]};'
            f'padding-bottom:4px;margin:24px 0 12px;">'
            f'{STATUS_LABELS[stage]}'
            f'<span style="font-weight:400;color:{COLORS["text_muted"]};margin-left:12px;">'
            f'{len(stage_df)}</span></div>',
            unsafe_allow_html=True,
        )

        if stage == "sourced":
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text_secondary"]};padding:8px 0 20px;line-height:1.6;">'
                f'{len(stage_df)} companies in the default sourced bucket. '
                f'Use <b>Deal Flow → Company Table</b> to browse and filter. '
                f'Move a company to a later stage to track it here.</div>',
                unsafe_allow_html=True,
            )
            continue

        hdr_cols = st.columns([3, 1, 1, 2, 2])
        for col, lbl in zip(hdr_cols, ["Company", "Score", "Tier", "Category", "Status"]):
            with col:
                st.markdown(
                    f'<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
                    f'text-transform:uppercase;color:{COLORS["text_secondary"]};padding-bottom:4px;">'
                    f'{lbl}</div>',
                    unsafe_allow_html=True,
                )

        for _, r in stage_df.iterrows():
            cid       = r["id"]
            score_str = f'{float(r["total_score"]):.2f}' if pd.notna(r.get("total_score")) else "—"
            tier_str  = f'T{int(r["tier"])}' if pd.notna(r.get("tier")) else "—"

            row_cols = st.columns([3, 1, 1, 2, 2])
            with row_cols[0]:
                if st.button(r["name"], key=f"pl_name_{cid}", use_container_width=True):
                    st.session_state.selected_company_id = int(cid)
                    st.session_state.page = "detail"
                    st.session_state.nav_origin = "pipeline"
                    st.rerun()
            with row_cols[1]:
                scored_at  = r.get("scored_at")
                scored_lbl = scored_at[:10] if scored_at else "unscored"
                st.markdown(
                    f'<div style="padding:4px 0;font-size:12px;font-weight:700;">'
                    f'{score_str}</div>'
                    f'<div style="font-size:10px;color:{COLORS["text_muted"]};margin-top:-1px;">'
                    f'{scored_lbl}</div>',
                    unsafe_allow_html=True,
                )
            with row_cols[2]:
                st.markdown(
                    f'<div style="padding:8px 0;font-size:12px;color:{COLORS["text_secondary"]};">'
                    f'{tier_str}</div>',
                    unsafe_allow_html=True,
                )
            with row_cols[3]:
                st.markdown(
                    f'<div style="padding:8px 0;font-size:10px;color:{COLORS["text_secondary"]};'
                    f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
                    f'{r.get("primary_category") or "—"}</div>',
                    unsafe_allow_html=True,
                )
            with row_cols[4]:
                new_s = st.selectbox(
                    "Status",
                    STATUS_STAGES,
                    index=STATUS_STAGES.index(stage),
                    key=f"pl_status_{cid}",
                    label_visibility="collapsed",
                    format_func=lambda s: STATUS_LABELS.get(s, s),
                )
                if new_s != stage:
                    _write(
                        "UPDATE companies SET status=?, status_updated_at=datetime('now') WHERE id=?",
                        (new_s, int(cid)),
                    )
                    st.cache_data.clear()
                    st.rerun()

            # Outreach expander
            outreach_status_val = r.get("outreach_status") or _DEFAULT_OUTREACH
            with st.expander(f"Outreach: {outreach_status_val}"):
                _render_outreach_panel(r)
