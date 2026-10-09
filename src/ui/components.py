"""
Reusable UI widgets. All colors imported from theme.py — no hardcoded hex codes.
"""
import re

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from ui.theme import COLORS


def force_scroll_top() -> None:
    """Force the parent app viewport to scroll to (0, 0). Multi-attempt to
    catch async-rendering charts and late-mount components. The 100/500/1000ms
    follow-ups bail out if the user has already interacted with the page so
    late-fires never yank the user back to top mid-scroll."""
    st.components.v1.html(
        """
        <script>
          (function () {
            const win = window.parent;
            const doc = win.document;
            let userInteracted = false;
            function markInteracted() { userInteracted = true; }
            const evs = ['scroll','wheel','touchstart','mousedown','keydown'];
            evs.forEach(function (e) {
              win.addEventListener(e, markInteracted, {passive: true, capture: true});
            });
            function scrollTop(force) {
              if (!force && userInteracted) return;
              try {
                win.scrollTo({top: 0, left: 0, behavior: 'instant'});
                doc.documentElement.scrollTop = 0;
                doc.body.scrollTop = 0;
                ['section.main','[data-testid="stAppViewContainer"]',
                 '[data-testid="stMain"]','.main','.block-container'
                ].forEach(function (s) {
                  doc.querySelectorAll(s).forEach(function (el) {
                    try { el.scrollTo(0, 0); } catch (e) {}
                    el.scrollTop = 0;
                  });
                });
              } catch (e) {}
            }
            scrollTop(true);
            setTimeout(function () { scrollTop(false); }, 100);
            setTimeout(function () { scrollTop(false); }, 500);
            setTimeout(function () {
              scrollTop(false);
              evs.forEach(function (e) {
                win.removeEventListener(e, markInteracted, {capture: true});
              });
            }, 1000);
          })();
        </script>
        """,
        height=0,
    )


def tier_badge(tier: int | None, label: str | None = None) -> str:
    styles = {
        1: f"background:{COLORS['tier1']};color:#ffffff;",
        2: f"background:{COLORS['tier2']};color:#ffffff;",
        3: f"background:{COLORS['tier3']};color:#ffffff;",
        4: f"background:{COLORS['tier4']};color:{COLORS['text']};",
    }
    display = label or (
        f"T{tier} — {['','STRONG CANDIDATE','WORTH WATCHING','EARLY SIGNAL','INSUFFICIENT DATA'][tier]}"
        if tier and tier <= 4 else "—"
    )
    s = styles.get(tier, f"background:{COLORS['tier4']};color:{COLORS['text']};")
    return (
        f'<span style="{s}padding:4px 12px;font-size:10px;font-weight:700;'
        f'letter-spacing:0.06em;font-family:Inter,sans-serif;">{display}</span>'
    )


def gap_badge(key: str) -> str:
    cfg = {
        "opportunity": ("1.5px solid #d97706", "#d97706", "OPPORTUNITY"),
        "open":        ("1.5px solid #dc2626", "#dc2626", "OPEN GAP"),
        "covered":     ("1.5px solid #16a34a", "#16a34a", "COVERED"),
    }
    border, color, label = cfg.get(key, ("1px solid #9ca3af", "#9ca3af", key.upper()))
    return (
        f'<span style="border:{border};color:{color};padding:4px 8px;'
        f'font-size:10px;font-weight:700;letter-spacing:0.08em;'
        f'font-family:Inter,sans-serif;">{label}</span>'
    )


def stat_html(
    label: str,
    value: str | int,
    note: str = "",
    delta: str | None = None,
    color_override: str | None = None,
) -> str:
    """Stat card. `delta` shows a small trend line under value; `color_override`
    swaps the value color (e.g. status-tinted counts)."""
    val_color = color_override or COLORS["text"]
    note_html = (
        f'<div style="font-size:12px;color:{COLORS["text_secondary"]};margin-top:4px;">{note}</div>'
        if note else ""
    )
    delta_html = (
        f'<div style="font-size:10px;color:{COLORS["text_muted"]};margin-top:4px;'
        f'font-weight:600;">{delta}</div>'
        if delta else ""
    )
    return f"""
<div style="background:{COLORS['card_bg']};border:1px solid {COLORS['card_border']};padding:20px 20px;">
  <div style="font-size:10px;font-weight:700;letter-spacing:0.12em;
              text-transform:uppercase;color:{COLORS['text_secondary']};">{label}</div>
  <div style="font-size:32px;font-weight:800;color:{val_color};margin-top:4px;
              font-family:Inter,sans-serif;">{value}</div>
  {delta_html}
  {note_html}
</div>"""


def category_badge(category: str, color: str | None = None) -> str:
    """Solid pill used for primary_category, sector tags, etc."""
    bg = color or COLORS["text"]
    return (
        f'<span style="background:{bg};color:#ffffff;padding:4px 12px;'
        f'font-size:10px;font-weight:700;letter-spacing:0.06em;'
        f'font-family:Inter,sans-serif;">{category}</span>'
    )


def outreach_badge(status: str) -> str:
    """Outreach-status pill. Color comes from data.OUTREACH_STATUS_COLORS."""
    from ui.data import OUTREACH_STATUS_COLORS
    color = OUTREACH_STATUS_COLORS.get(status or "Not Started", COLORS["text_secondary"])
    return (
        f'<span style="background:{color};color:#ffffff;font-size:10px;'
        f'font-weight:700;padding:4px 8px;border-radius:3px;'
        f'letter-spacing:0.04em;white-space:nowrap;'
        f'font-family:Inter,sans-serif;">{status}</span>'
    )


_CONF_HIGH = 0.8
_CONF_MID = 0.5


def _confidence_color(score: float | None) -> str:
    """Return a hex color for a confidence score in [0,1]. None → light gray."""
    if score is None:
        return COLORS["border_light"]
    if score >= _CONF_HIGH:
        return COLORS["gap_covered"]
    if score >= _CONF_MID:
        return COLORS["gap_opportunity"]
    return COLORS["text_muted"]


def confidence_dot(score: float | None, label: str | None = None) -> str:
    """Inline 8px dot whose color reflects extraction confidence.

    None        → empty/gray  (no data)
    0.0-0.5     → muted gray  (low / speculative)
    0.5-0.8     → amber       (medium / inferred)
    0.8-1.0     → green       (high / directly stated)

    Hover tooltip shows the numeric score plus an optional label.
    """
    color = _confidence_color(score)
    if score is None:
        title = f"{label}: no data" if label else "No data"
    else:
        bucket = "high" if score >= _CONF_HIGH else "medium" if score >= _CONF_MID else "low"
        title = (
            f"{label}: confidence {score:.2f} ({bucket})"
            if label else f"Confidence {score:.2f} ({bucket})"
        )
    return (
        f'<span title="{title}" '
        f'style="display:inline-block;width:8px;height:8px;border-radius:50%;'
        f'background:{color};margin-right:8px;vertical-align:middle;"></span>'
    )


def confidence_tag(score: float | None) -> str:
    """Returns a small '[unverified]' marker for low-confidence values, else empty."""
    if score is None or score >= _CONF_MID:
        return ""
    return (
        f'<span style="font-size:10px;color:{COLORS["text_muted"]};'
        f'font-style:italic;margin-left:8px;">[unverified]</span>'
    )


_CONTACT_TYPE_LABELS = {
    "general_email": "EMAIL",
    "sales_email":   "SALES",
    "phone":         "PHONE",
    "twitter":       "TWITTER",
    "linkedin":      "LINKEDIN",
}

_CONTACT_TYPE_COLORS = {
    "general_email": "#374151",
    "sales_email":   "#059669",
    "phone":         "#2563eb",
    "twitter":       "#1da1f2",
    "linkedin":      "#0a66c2",
}


def contact_type_badge(ctype: str) -> str:
    label = _CONTACT_TYPE_LABELS.get(ctype, ctype.upper())
    bg    = _CONTACT_TYPE_COLORS.get(ctype, COLORS["text"])
    return (
        f'<span style="background:{bg};color:#ffffff;font-size:10px;'
        f'font-weight:700;letter-spacing:0.06em;padding:4px 8px;'
        f'font-family:Inter,sans-serif;">{label}</span>'
    )


def _contact_value_link(ctype: str, value: str) -> str:
    """Wrap the contact value in an appropriate link tag."""
    safe = (value or "").strip()
    if not safe:
        return "—"
    if ctype in ("general_email", "sales_email"):
        return f'<a href="mailto:{safe}" style="color:{COLORS["text"]};">{safe}</a>'
    if ctype == "phone":
        digits = "".join(ch for ch in safe if ch.isdigit() or ch == "+")
        return f'<a href="tel:{digits}" style="color:{COLORS["text"]};">{safe}</a>'
    if ctype == "twitter":
        handle = safe.lstrip("@")
        return (
            f'<a href="https://twitter.com/{handle}" target="_blank" '
            f'style="color:{COLORS["text"]};">@{handle}</a>'
        )
    if ctype == "linkedin":
        href = safe if safe.startswith("http") else f"https://{safe}"
        return f'<a href="{href}" target="_blank" style="color:{COLORS["text"]};">{safe}</a>'
    return safe


def founder_card(founder: dict) -> str:
    """Single-founder card with name, role, bio, background, links, confidence dot."""
    name = (founder.get("name") or "").strip() or "—"
    role = founder.get("role") or ""
    bio = (founder.get("bio") or "").strip()
    if len(bio) > 200:
        bio = bio[:197].rstrip() + "…"
    background = (founder.get("background") or "").strip()
    linkedin   = (founder.get("linkedin_url") or "").strip()
    twitter    = (founder.get("twitter_handle") or "").strip()
    score      = founder.get("confidence")

    role_html = (
        f'<div style="font-size:10px;color:{COLORS["text_secondary"]};'
        f'margin-top:2px;letter-spacing:0.04em;text-transform:uppercase;'
        f'font-weight:700;">{role}</div>'
    ) if role else ""

    bio_html = (
        f'<div style="font-size:12px;color:{COLORS["text"]};'
        f'margin-top:8px;line-height:1.5;">{bio}</div>'
    ) if bio else ""

    bg_html = (
        f'<div style="font-size:10px;color:{COLORS["text_secondary"]};'
        f'margin-top:8px;font-style:italic;">{background}</div>'
    ) if background else ""

    links: list[str] = []
    if linkedin:
        href = linkedin if linkedin.startswith("http") else f"https://{linkedin}"
        links.append(
            f'<a href="{href}" target="_blank" '
            f'style="font-size:10px;color:#0a66c2;font-weight:700;'
            f'letter-spacing:0.04em;">LINKEDIN</a>'
        )
    if twitter:
        handle = twitter.lstrip("@")
        links.append(
            f'<a href="https://twitter.com/{handle}" target="_blank" '
            f'style="font-size:10px;color:#1da1f2;font-weight:700;'
            f'letter-spacing:0.04em;">@{handle}</a>'
        )
    links_html = (
        f'<div style="margin-top:12px;display:flex;gap:12px;flex-wrap:wrap;">'
        f'{"".join(links)}</div>'
    ) if links else ""

    dot = _confidence_color(score)
    score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "—"

    return (
        f'<div style="background:{COLORS["card_bg"]};'
        f'border:1px solid {COLORS["card_border"]};padding:16px;'
        f'position:relative;height:100%;">'
        f'<div title="Confidence {score_str}" '
        f'style="position:absolute;top:12px;right:12px;width:8px;height:8px;'
        f'border-radius:50%;background:{dot};"></div>'
        f'<div style="font-size:14px;font-weight:800;color:{COLORS["text"]};'
        f'padding-right:20px;line-height:1.3;">{name}</div>'
        f'{role_html}{bio_html}{bg_html}{links_html}'
        f'</div>'
    )


def contact_row(contact: dict) -> str:
    """One-line contact entry with type badge, value link, confidence dot."""
    ctype = (contact.get("type") or "").strip()
    value = (contact.get("value") or "").strip()
    score = contact.get("confidence")
    return (
        f'<div style="display:flex;align-items:center;gap:12px;padding:8px 0;'
        f'border-bottom:1px solid {COLORS["border_light"]};">'
        f'{confidence_dot(score, label=_CONTACT_TYPE_LABELS.get(ctype, ctype))}'
        f'{contact_type_badge(ctype)}'
        f'<span style="font-size:12px;color:{COLORS["text"]};flex:1;'
        f'word-break:break-all;">{_contact_value_link(ctype, value)}</span>'
        f'</div>'
    )


def stealth_badge() -> str:
    """Orange STEALTH pill, same dimensions as category_badge."""
    return (
        f'<span style="background:{COLORS["gap_opportunity"]};color:#ffffff;'
        f'padding:4px 12px;font-size:10px;font-weight:700;letter-spacing:0.06em;'
        f'font-family:Inter,sans-serif;">STEALTH</span>'
    )


def page_title(text: str, subtitle: str | None = None) -> None:
    """Consistent page-level h1. Optional subtitle in muted text below."""
    st.markdown(
        f'<h1 style="font-size:20px;font-weight:800;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:{COLORS["text"]};margin:0 0 4px;">'
        f'{text}</h1>',
        unsafe_allow_html=True,
    )
    if subtitle:
        st.markdown(
            f'<p style="font-size:12px;color:{COLORS["text_muted"]};'
            f'margin:0 0 20px;">{subtitle}</p>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<div style="margin-bottom:20px;"></div>', unsafe_allow_html=True)


def section_header(text: str, margin_top: int = 24) -> None:
    st.markdown(
        f'<div style="font-size:12px;font-weight:800;letter-spacing:0.1em;'
        f'text-transform:uppercase;border-bottom:1.5px solid {COLORS["text"]};'
        f'padding-bottom:8px;margin:{margin_top}px 0 14px;color:{COLORS["text"]};">'
        f'{text}</div>',
        unsafe_allow_html=True,
    )


def slug(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name.lower())


def radar_chart(breakdown: dict) -> "alt.Chart | None":
    if not breakdown:
        return None
    DIM_ORDER = [
        "defense_relevance", "technical_founders", "post_war_durable",
        "nato_exportable", "stage_fit", "shipped_product",
    ]
    DIM_SHORT = {
        "defense_relevance":  "Defense",
        "technical_founders": "Tech Founders",
        "post_war_durable":   "Post-War",
        "nato_exportable":    "NATO",
        "stage_fit":          "Stage Fit",
        "shipped_product":    "Shipped",
    }
    dims = [d for d in DIM_ORDER if d in breakdown]
    if len(dims) < 3:
        return None
    n      = len(dims)
    R_MAX  = 100
    angles = [2 * np.pi * i / n - np.pi / 2 for i in range(n)]

    pts = []
    for i, d in enumerate(dims):
        r = (breakdown[d]["score"] / 3.0) * R_MAX
        pts.append({
            "x": r * np.cos(angles[i]),
            "y": r * np.sin(angles[i]),
            "dim": DIM_SHORT.get(d, d),
            "score": breakdown[d]["score"],
        })
    pts.append({**pts[0], "dim": pts[0]["dim"]})

    grid_pts = []
    for lvl in [1, 2, 3]:
        r = (lvl / 3.0) * R_MAX
        for theta in np.linspace(0, 2 * np.pi, 60):
            grid_pts.append({"x": r * np.cos(theta), "y": r * np.sin(theta), "lvl": str(lvl)})

    axis_pts = [
        {"x0": 0.0, "y0": 0.0,
         "x1": R_MAX * np.cos(angles[i]),
         "y1": R_MAX * np.sin(angles[i])}
        for i in range(n)
    ]
    label_pts = [
        {"x": R_MAX * 1.30 * np.cos(angles[i]),
         "y": R_MAX * 1.30 * np.sin(angles[i]),
         "label": DIM_SHORT.get(dims[i], dims[i])}
        for i in range(n)
    ]

    dom = [-145, 145]
    sc  = {"domain": dom}

    grid_layer = (
        alt.Chart(pd.DataFrame(grid_pts))
        .mark_line(color="#e5e7eb", strokeWidth=0.8)
        .encode(
            x=alt.X("x:Q", axis=None, scale=alt.Scale(**sc)),
            y=alt.Y("y:Q", axis=None, scale=alt.Scale(**sc)),
            detail="lvl:N",
        )
    )
    axis_layer = (
        alt.Chart(pd.DataFrame(axis_pts))
        .mark_rule(color="#d1d5db", strokeWidth=0.8)
        .encode(x="x0:Q", y="y0:Q", x2="x1:Q", y2="y1:Q")
    )
    score_df   = pd.DataFrame(pts)
    fill_layer = (
        alt.Chart(score_df)
        .mark_area(color="#111111", opacity=0.15)
        .encode(x="x:Q", y="y:Q")
    )
    line_layer = (
        alt.Chart(score_df)
        .mark_line(color="#111111", strokeWidth=2)
        .encode(x="x:Q", y="y:Q")
    )
    dot_layer = (
        alt.Chart(score_df.iloc[:-1])
        .mark_point(color="#111111", size=50, filled=True)
        .encode(
            x="x:Q", y="y:Q",
            tooltip=[alt.Tooltip("dim:N", title="Dimension"),
                     alt.Tooltip("score:Q", title="Score")],
        )
    )
    label_layer = (
        alt.Chart(pd.DataFrame(label_pts))
        .mark_text(fontSize=9, color="#374151")
        .encode(x="x:Q", y="y:Q", text="label:N")
    )

    return (
        alt.layer(grid_layer, axis_layer, fill_layer, line_layer, dot_layer, label_layer)
        .properties(width=230, height=230, background="white")
        .configure_view(strokeWidth=0, fill="white")
    )
