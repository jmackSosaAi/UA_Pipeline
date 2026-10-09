"""
PRESS tab — defense industry coverage and company mentions.

Renders articles from processed_articles with filtering, sorting, and
clickable links to mentioned companies (via article_companies).
"""

import html
from datetime import datetime

import streamlit as st

from collectors.vocabulary import get_all_sector_keys
from ui.components import force_scroll_top, page_title, section_header, stat_html
from ui.data import (
    count_articles,
    load_articles,
    load_press_metrics,
    load_publications,
)
from ui.theme import COLORS

_PAGE_SIZE = 20

_DATE_RANGE_OPTIONS = [
    ("7d",  "Last 7 days"),
    ("30d", "Last 30 days"),
    ("90d", "Last 90 days"),
    ("all", "All time"),
]


# ── Filter state helpers ──────────────────────────────────────────────────────


def _filters_from_state() -> dict:
    return {
        "publications":  st.session_state.get("press_publications", []) or [],
        "sectors":       st.session_state.get("press_sectors", []) or [],
        "min_relevance": float(st.session_state.get("press_min_relevance", 0.0) or 0.0),
        "date_range":    st.session_state.get("press_date_range", "all") or "all",
    }


def _filters_signature() -> tuple:
    """Snapshot of all filter values — compared against last-render to reset offset."""
    f = _filters_from_state()
    return (
        tuple(f["publications"]),
        tuple(f["sectors"]),
        round(f["min_relevance"], 4),
        f["date_range"],
    )


# ── Card sub-renderers ────────────────────────────────────────────────────────


def _relevance_badge(score: float | None) -> str:
    if score is None:
        return ""
    if score >= 0.7:
        bg = COLORS["gap_covered"]
    elif score >= 0.4:
        bg = COLORS["gap_opportunity"]
    else:
        bg = COLORS["text_muted"]
    return (
        f'<span style="background:{bg};color:#ffffff;padding:3px 10px;'
        f'font-size:10px;font-weight:700;letter-spacing:0.06em;'
        f'font-family:Inter,sans-serif;">RELEVANCE {score:.2f}</span>'
    )


def _sector_pill(tag: str) -> str:
    return (
        f'<span style="background:transparent;color:{COLORS["text_secondary"]};'
        f'border:1px solid {COLORS["border_mid"]};padding:2px 8px;'
        f'font-size:10px;font-weight:600;letter-spacing:0.04em;'
        f'font-family:Inter,monospace;margin-right:6px;display:inline-block;">'
        f'{html.escape(tag)}</span>'
    )


def _format_date(value) -> str:
    if not value:
        return ""
    try:
        return str(value).split(" ")[0]   # YYYY-MM-DD prefix is enough
    except Exception:
        return str(value)


def _render_article_card(article: dict) -> None:
    """Header (title + meta) + summary + sector pills + company chips + footer."""
    title = (article.get("title") or "(untitled)").strip()
    url = article.get("url") or ""
    pub = article.get("publication") or "—"
    date_str = _format_date(article.get("published_at") or article.get("processed_at"))
    summary = (article.get("summary") or "").strip()
    rel = article.get("relevance_score")
    tags = article.get("sector_tags_list") or []
    byline = (article.get("byline") or "").strip()
    companies = article.get("companies") or []

    # Header row: title (link), publication+date, relevance badge — pure HTML.
    safe_title = html.escape(title)
    safe_url = html.escape(url)
    rel_badge = _relevance_badge(rel) if rel is not None else ""
    pub_meta = (
        f'<span style="font-size:11px;color:{COLORS["text_muted"]};'
        f'letter-spacing:0.04em;text-transform:uppercase;font-weight:700;">'
        f'{html.escape(pub)}</span>'
        + (
            f' &nbsp;·&nbsp; <span style="font-size:11px;color:{COLORS["text_muted"]};">'
            f'{html.escape(date_str)}</span>' if date_str else ""
        )
    )

    tags_html = "".join(_sector_pill(t) for t in tags) if tags else ""
    summary_html = (
        f'<div style="font-size:12px;color:{COLORS["text"]};line-height:1.5;'
        f'margin-top:8px;">{html.escape(summary)}</div>'
        if summary else ""
    )
    byline_html = (
        f'<div style="font-size:10px;color:{COLORS["text_muted"]};font-style:italic;'
        f'margin-top:6px;">By {html.escape(byline)}</div>'
        if byline else ""
    )
    url_html = (
        f'<div style="margin-top:6px;"><a href="{safe_url}" target="_blank" '
        f'style="font-size:10px;color:{COLORS["text_muted"]};word-break:break-all;'
        f'text-decoration:underline;">{safe_url}</a></div>'
        if url else ""
    )

    st.markdown(
        f"""
<div style="background:{COLORS['card_bg']};border:1px solid {COLORS['card_border']};
            padding:16px;margin-bottom:12px;">
  <div style="display:flex;gap:12px;align-items:flex-start;justify-content:space-between;">
    <div style="flex:1;min-width:0;">
      <a href="{safe_url}" target="_blank" style="font-size:14px;font-weight:800;
          color:{COLORS['text']};line-height:1.3;text-decoration:none;display:block;">
        {safe_title}
      </a>
      <div style="margin-top:4px;">{pub_meta}</div>
    </div>
    <div style="flex-shrink:0;">{rel_badge}</div>
  </div>
  {f'<div style="margin-top:10px;">{tags_html}</div>' if tags_html else ''}
  {summary_html}
  {byline_html}
  {url_html}
</div>
""",
        unsafe_allow_html=True,
    )

    # Companies row — buttons must be real Streamlit widgets to be clickable.
    if companies:
        _render_company_chips(article["id"], companies)


def _render_company_chips(article_id: int, companies: list[dict]) -> None:
    """Up to 6 clickable chips + '+N more' if truncated. Unpromoted leads
    render as a muted, non-clickable pill so the user sees the mention exists."""
    visible = companies[:6]
    extra = max(0, len(companies) - len(visible))
    n_cols = len(visible) + (1 if extra else 0)
    if n_cols == 0:
        return
    cols = st.columns(n_cols)

    for i, comp in enumerate(visible):
        cid = comp.get("company_id")
        name = comp.get("company_name") or "?"
        with cols[i]:
            if cid:
                if st.button(
                    name,
                    key=f"press_chip_{article_id}_{i}",
                    use_container_width=True,
                ):
                    st.session_state["selected_company_id"] = int(cid)
                    st.session_state["page"] = "detail"
                    st.session_state["nav_origin"] = "press"
                    st.rerun()
            else:
                st.markdown(
                    f'<div title="Lead exists but not yet promoted to a company" '
                    f'style="background:transparent;color:{COLORS["text_muted"]};'
                    f'border:1px dashed {COLORS["border_mid"]};padding:7px 12px;'
                    f'font-size:11px;font-weight:600;letter-spacing:0.04em;'
                    f'text-align:center;font-family:Inter,sans-serif;'
                    f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
                    f'{html.escape(name)}</div>',
                    unsafe_allow_html=True,
                )

    if extra:
        with cols[-1]:
            st.markdown(
                f'<div style="padding:7px 12px;font-size:11px;color:{COLORS["text_muted"]};'
                f'font-weight:700;letter-spacing:0.04em;text-align:center;">+{extra} more</div>',
                unsafe_allow_html=True,
            )


# ── Filters panel ─────────────────────────────────────────────────────────────


def _render_filters() -> None:
    pubs_options = load_publications()
    sector_options = get_all_sector_keys()

    cols = st.columns([3, 3, 2, 2])
    with cols[0]:
        st.multiselect(
            "Publication",
            options=pubs_options,
            key="press_publications",
            placeholder="All publications",
        )
    with cols[1]:
        st.multiselect(
            "Sector",
            options=sector_options,
            key="press_sectors",
            placeholder="All sectors",
        )
    with cols[2]:
        st.slider(
            "Min relevance",
            min_value=0.0, max_value=1.0, step=0.05,
            key="press_min_relevance",
        )
    with cols[3]:
        st.radio(
            "Date range",
            options=[k for k, _ in _DATE_RANGE_OPTIONS],
            format_func=lambda k: dict(_DATE_RANGE_OPTIONS)[k],
            key="press_date_range",
            horizontal=False,
        )


# ── Pagination ────────────────────────────────────────────────────────────────


def _render_pagination(total: int) -> None:
    offset = int(st.session_state.get("press_offset", 0))
    page_idx = (offset // _PAGE_SIZE) + 1
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    cols = st.columns([1, 4, 1])
    with cols[0]:
        if st.button("← PREV", key="press_prev", disabled=offset == 0):
            st.session_state["press_offset"] = max(0, offset - _PAGE_SIZE)
            st.rerun()
    with cols[1]:
        st.markdown(
            f'<div style="text-align:center;font-size:11px;color:{COLORS["text_muted"]};'
            f'padding-top:8px;letter-spacing:0.04em;">PAGE {page_idx} OF {total_pages} '
            f'&nbsp;·&nbsp; {total} ARTICLES</div>',
            unsafe_allow_html=True,
        )
    with cols[2]:
        is_last = (offset + _PAGE_SIZE) >= total
        if st.button("NEXT →", key="press_next", disabled=is_last):
            st.session_state["press_offset"] = offset + _PAGE_SIZE
            st.rerun()


# ── Page entrypoint ───────────────────────────────────────────────────────────


def render() -> None:
    force_scroll_top()

    # Initialise session keys (do NOT overwrite if widgets already wrote them)
    for k, v in [
        ("press_publications",  []),
        ("press_sectors",       []),
        ("press_min_relevance", 0.0),
        ("press_date_range",    "all"),
        ("press_offset",        0),
    ]:
        if k not in st.session_state:
            st.session_state[k] = v

    # Reset pagination whenever filters change.
    sig = _filters_signature()
    if st.session_state.get("press_filter_sig") != sig:
        st.session_state["press_filter_sig"] = sig
        st.session_state["press_offset"] = 0

    page_title("PRESS", "Defense industry coverage and company mentions")

    # ── Top metrics ──
    metrics = load_press_metrics()
    m_cols = st.columns(4)
    with m_cols[0]:
        st.markdown(stat_html("TOTAL ARTICLES", f"{metrics['total_articles']:,}"), unsafe_allow_html=True)
    with m_cols[1]:
        st.markdown(stat_html("LAST 7 DAYS", f"{metrics['articles_last_7d']:,}"), unsafe_allow_html=True)
    with m_cols[2]:
        st.markdown(stat_html("COMPANIES MENTIONED", f"{metrics['total_companies_mentioned']:,}"), unsafe_allow_html=True)
    with m_cols[3]:
        st.markdown(stat_html("MENTIONS LAST 30D", f"{metrics['mentions_last_30d']:,}"), unsafe_allow_html=True)

    # ── Filters ──
    section_header("FILTERS")
    _render_filters()

    # ── Article list ──
    section_header("ARTICLES")
    filters = _filters_from_state()
    offset = int(st.session_state.get("press_offset", 0))
    articles = load_articles(filters, limit=_PAGE_SIZE, offset=offset)
    total = count_articles(filters)

    if not articles:
        st.markdown(
            f'<div style="padding:24px;background:{COLORS["card_bg"]};'
            f'border:1px solid {COLORS["border_light"]};text-align:center;'
            f'color:{COLORS["text_muted"]};font-size:12px;">'
            f'No articles found. Try adjusting filters.</div>',
            unsafe_allow_html=True,
        )
        return

    for art in articles:
        _render_article_card(art)

    st.markdown('<div style="margin-top:16px;"></div>', unsafe_allow_html=True)
    _render_pagination(total)
