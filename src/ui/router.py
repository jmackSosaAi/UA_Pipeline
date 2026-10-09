"""
Navigation router — single source of truth for page state and sidebar rendering.
No other file writes to st.session_state['page'].
"""
import streamlit as st

from ui.data import (
    STATUS_LABELS,
    STATUS_STAGES,
    _read,
    load_companies,
    load_company,
    search_companies,
)
from ui.theme import COLORS

# ── Session-state defaults ────────────────────────────────────────────────────

def _init() -> None:
    for k, v in [
        ("page",                  "home"),
        ("selected_company_id",   None),
        ("portfolio_selected_id", None),
    ]:
        if k not in st.session_state:
            st.session_state[k] = v


def get_current_page() -> str:
    _init()
    return st.session_state["page"]


# ── Sidebar ───────────────────────────────────────────────────────────────────

_MATCH_LABELS = {
    "name":        "matched in: name",
    "founders":    "matched in: founders",
    "category":    "matched in: category",
    "sector":      "matched in: sector",
    "country":     "matched in: country",
    "description": "matched in: description",
}


def _render_search() -> None:
    """Sidebar global search: input + ranked dropdown of clickable results."""
    # Streamlit forbids mutating a widget's session_state value after the
    # widget has rendered. To clear the search box on result-click we set a
    # flag during the click handler, then honor it on the NEXT render before
    # the text_input mounts.
    if st.session_state.get("_clear_search"):
        st.session_state["sidebar_search"] = ""
        st.session_state["_clear_search"] = False

    query = st.text_input(
        "Search",
        key="sidebar_search",
        placeholder="Search companies, founders, sectors...",
        label_visibility="collapsed",
    )
    q = (query or "").strip()
    if not q:
        return

    results = search_companies(q, limit=10)
    n = len(results)
    if n == 0:
        st.markdown(
            f'<div style="font-size:10px;color:{COLORS["text_muted"]};'
            f'padding:8px 0 4px;letter-spacing:0.04em;">No matches</div>',
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        f'<div style="font-size:10px;color:{COLORS["text_muted"]};'
        f'padding:6px 0 4px;letter-spacing:0.06em;text-transform:uppercase;'
        f'font-weight:700;">{n} result{"s" if n != 1 else ""}</div>',
        unsafe_allow_html=True,
    )

    for r in results:
        cid       = r["id"]
        cname     = r["name"]
        score     = r.get("score")
        tier      = r.get("tier")
        category  = r.get("category") or "—"
        match_fld = r.get("match_field") or "name"

        score_str = f"{float(score):.2f}" if score is not None else "—"
        tier_str  = f"T{tier}" if tier else "—"

        st.markdown(
            f'<div style="font-size:11px;font-weight:800;color:{COLORS["text"]};'
            f'margin:8px 0 2px;line-height:1.3;">{cname}</div>'
            f'<div style="font-size:9px;color:{COLORS["text_secondary"]};'
            f'margin-bottom:2px;">'
            f'<b>{score_str}</b>&nbsp;·&nbsp;{tier_str}&nbsp;·&nbsp;{category}'
            f'</div>'
            f'<div style="font-size:9px;color:{COLORS["text_muted"]};'
            f'font-style:italic;margin-bottom:4px;">'
            f'{_MATCH_LABELS.get(match_fld, "matched")}'
            f'</div>',
            unsafe_allow_html=True,
        )
        if st.button(
            "OPEN",
            key=f"search_open_{cid}",
            use_container_width=True,
        ):
            st.session_state["selected_company_id"] = cid
            st.session_state["page"] = "detail"
            st.session_state["nav_origin"] = None
            st.session_state["_clear_search"] = True
            st.rerun()


def render_sidebar() -> None:
    _init()

    with st.sidebar:
        # ── Logo ──────────────────────────────────────────────────────────────
        st.markdown("""
<div style="padding:16px 16px 12px;margin:0;">
  <div style="display:inline-block;background:#222;border-radius:8px;
              padding:12px 20px;margin-bottom:8px;">
    <span style="font-family:'Inter',sans-serif;font-weight:800;font-size:26px;
                 color:white;letter-spacing:1px;">UA PIPELINE</span>
  </div>
  <div style="font-family:'Inter',sans-serif;font-weight:600;font-size:10px;
              color:#222;letter-spacing:2px;text-transform:uppercase;margin-top:4px;">
    DEFENSE TECH
  </div>
  <div style="font-family:'Inter',sans-serif;font-weight:400;font-size:10px;
              color:{COLORS["text_muted"]};letter-spacing:2px;text-transform:uppercase;margin-top:8px;">
    SOURCING TOOL
  </div>
</div>
""", unsafe_allow_html=True)

        st.markdown(
            f'<hr style="border:none;border-top:1px solid {COLORS["border_light"]};margin:8px 0 16px;">',
            unsafe_allow_html=True,
        )

        # ── Global search ────────────────────────────────────────────────────
        _render_search()

        st.markdown(
            f'<hr style="border:none;border-top:1px solid {COLORS["border_light"]};margin:12px 0 16px;">',
            unsafe_allow_html=True,
        )

        # ── Nav buttons ──────────────────────────────────────────────────────
        current = st.session_state["page"]

        nav_items = [
            ("HOME",          "home"),
            ("PORTFOLIO",     "portfolio"),
            ("INDUSTRY DATA", "industry"),
            ("DEAL FLOW",     "overview"),
            ("GAP ANALYSIS",  "gap"),
            ("PRESS",         "press"),
            ("PIPELINE",      "pipeline"),
        ]
        for label, key in nav_items:
            is_active = current == key or (current == "detail" and key == "overview")
            btn_label = f"▶  {label}" if is_active else f"    {label}"
            if st.button(btn_label, key=f"nav_{key}", use_container_width=True):
                st.session_state["page"] = key
                st.session_state["selected_company_id"] = None
                st.rerun()

        st.markdown(
            f'<hr style="border:none;border-top:1px solid {COLORS["border_light"]};margin:16px 0 12px;">',
            unsafe_allow_html=True,
        )

        # ── Deal flow filters ─────────────────────────────────────────────────
        st.markdown(
            '<div style="font-size:10px;font-weight:700;letter-spacing:0.1em;'
            f'text-transform:uppercase;color:{COLORS["text_muted"]};margin-bottom:8px;">DEAL FLOW FILTERS</div>',
            unsafe_allow_html=True,
        )
        _df_filt = load_companies()
        _cats_opts = (
            ["All"] + sorted(_df_filt["primary_category"].dropna().unique().tolist())
            if not _df_filt.empty else ["All"]
        )
        _src_opts = (
            ["All"] + sorted(_df_filt["source"].dropna().unique().tolist())
            if not _df_filt.empty else ["All"]
        )
        st.selectbox("Tier", ["All", 1, 2, 3, 4], key="ov_tier")
        st.selectbox("Category", _cats_opts, key="ov_cat")
        st.selectbox("Source", _src_opts, key="ov_src")
        st.selectbox(
            "Status",
            ["All"] + STATUS_STAGES,
            key="ov_status",
            format_func=lambda s: STATUS_LABELS.get(s, s) if s != "All" else "All",
        )
        st.checkbox(
            "Show unenriched companies",
            value=False,
            key="ov_show_unenriched",
            help="When off, hides companies with no description, dossier, or enrichment data.",
        )
        try:
            _show_unr = st.session_state.get("ov_show_unenriched", False)
            _tier_v   = st.session_state.get("ov_tier", "All")
            _cat_v    = st.session_state.get("ov_cat", "All")
            _src_v    = st.session_state.get("ov_src", "All")
            _st_v     = st.session_state.get("ov_status", "All")
            _filt_df  = _df_filt.copy()
            if not _show_unr and not _filt_df.empty:
                _hd = (
                    _filt_df["description"].notna()
                    | _filt_df["enriched_at"].notna()
                    | _filt_df["dossier_at"].notna()
                )
                _filt_df = _filt_df[_hd]
            if _tier_v != "All" and not _filt_df.empty:
                _filt_df = _filt_df[_filt_df["tier"] == _tier_v]
            if _cat_v  != "All" and not _filt_df.empty:
                _filt_df = _filt_df[_filt_df["primary_category"] == _cat_v]
            if _src_v  != "All" and not _filt_df.empty:
                _filt_df = _filt_df[_filt_df["source"] == _src_v]
            if _st_v   != "All" and not _filt_df.empty:
                _filt_df = _filt_df[_filt_df["status"] == _st_v]
            st.markdown(
                f'<div style="font-size:10px;color:{COLORS["text_muted"]};margin-top:8px;">'
                f'{len(_filt_df)} shown / {len(_df_filt)} total</div>',
                unsafe_allow_html=True,
            )
        except Exception:
            pass

        # ── Viewing context (detail page) ────────────────────────────────────
        if current == "detail" and st.session_state.get("selected_company_id"):
            _row = load_company(st.session_state["selected_company_id"])
            if _row:
                _tier  = _row.get("tier")
                _score = float(_row.get("total_score") or 0)
                st.markdown(
                    f'<div style="margin-top:20px;padding:12px;background:{COLORS["card_bg"]};'
                    f'border:1px solid {COLORS["border_mid"]};">'
                    f'<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
                    f'text-transform:uppercase;color:{COLORS["text_muted"]};margin-bottom:4px;">VIEWING</div>'
                    f'<div style="font-size:12px;font-weight:800;color:{COLORS["text"]};'
                    f'word-break:break-word;">{_row["name"].upper()}</div>'
                    f'<div style="font-size:10px;color:{COLORS["text_secondary"]};margin-top:4px;">'
                    f'Score {_score:.2f} &nbsp;·&nbsp; '
                    f'{"T"+str(_tier) if _tier else "—"}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        st.markdown(
            f'<hr style="border:none;border-top:1px solid {COLORS["border_light"]};margin:20px 0 12px;">',
            unsafe_allow_html=True,
        )
        try:
            _tot  = _read(
                "SELECT COUNT(*) AS n FROM companies "
                "WHERE portfolio_company IS NULL OR portfolio_company=0"
            )[0]["n"]
            _excl = _read(
                "SELECT COUNT(*) AS n FROM companies WHERE portfolio_company=1"
            )[0]["n"]
            st.markdown(
                f'<div style="font-size:10px;color:{COLORS["text_muted"]};letter-spacing:0.06em;">'
                f'{_tot} ELIGIBLE &nbsp;·&nbsp; {_excl} EXCLUDED</div>',
                unsafe_allow_html=True,
            )
        except Exception:
            pass
