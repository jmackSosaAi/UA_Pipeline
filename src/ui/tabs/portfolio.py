import streamlit as st

from ui.components import category_badge, force_scroll_top, page_title, section_header, stealth_badge
from ui.data import load_companies, load_portfolio
from ui.theme import COLORS


def render(conn=None) -> None:
    companies  = load_portfolio()
    sel_id     = st.session_state.get("portfolio_selected_id")

    # ── DETAIL VIEW ────────────────────────────────────────────────
    if sel_id:
        co = next((c for c in companies if c["id"] == sel_id), None)
        if not co:
            st.session_state.portfolio_selected_id = None
            return

        force_scroll_top()

        if st.button("← BACK TO PORTFOLIO", key="port_back"):
            st.session_state.portfolio_selected_id = None
            st.rerun()

        name_up = co["name"].upper()
        st.markdown(
            f'<h1 style="font-size:20px;margin:12px 0 8px;">{name_up}</h1>',
            unsafe_allow_html=True,
        )
        pills = ""
        if co.get("category_tag"):
            for tag in co["category_tag"].split("·"):
                pills += category_badge(tag.strip()) + "&nbsp;"
        if co.get("is_stealth"):
            pills += stealth_badge() + "&nbsp;"
        meta = ""
        if co.get("location"):
            meta += f'<span style="font-size:12px;color:{COLORS["text_secondary"]};margin-right:16px;">{co["location"]}</span>'
        if co.get("founded_year"):
            meta += f'<span style="font-size:12px;color:{COLORS["text_secondary"]};margin-right:16px;">Founded {co["founded_year"]}</span>'
        if co.get("website"):
            meta += (
                f'<a href="{co["website"]}" target="_blank" '
                f'style="font-size:12px;color:#374151;font-weight:600;">'
                f'{co["website"].replace("https://","").replace("http://","")}</a>'
            )
        st.markdown(pills + ("<br>" if meta else "") + meta, unsafe_allow_html=True)

        st.markdown(
            f'<hr style="border:none;border-top:1px solid {COLORS["border_light"]};margin:16px 0;">',
            unsafe_allow_html=True,
        )

        left, right = st.columns([3, 2])

        with left:
            section_header("ABOUT", margin_top=0)
            st.markdown(
                f'<div style="font-size:12px;color:{COLORS["text"]};line-height:1.7;">'
                f'{co.get("full_description") or co.get("description", "")}</div>',
                unsafe_allow_html=True,
            )

            if co.get("products"):
                section_header("PRODUCTS")
                for p in co["products"]:
                    st.markdown(
                        f'<div style="margin-bottom:12px;padding:12px 16px;'
                        f'background:{COLORS["card_bg"]};border-left:3px solid {COLORS["text"]};">'
                        f'<div style="font-size:12px;font-weight:800;letter-spacing:0.05em;'
                        f'text-transform:uppercase;color:{COLORS["text"]};">{p["name"]}</div>'
                        f'<div style="font-size:12px;color:#374151;margin-top:4px;line-height:1.5;">'
                        f'{p["description"]}</div></div>',
                        unsafe_allow_html=True,
                    )

            if co.get("traction"):
                section_header("TRACTION & DEPLOYMENTS")
                st.markdown(
                    f'<div style="font-size:12px;color:#374151;line-height:1.7;">{co["traction"]}</div>',
                    unsafe_allow_html=True,
                )

        with right:
            if co.get("team"):
                section_header("TEAM", margin_top=0)
                for member in co["team"]:
                    st.markdown(
                        f'<div style="margin-bottom:8px;">'
                        f'<span style="font-size:12px;font-weight:700;color:{COLORS["text"]};">{member["name"]}</span>'
                        f'<span style="font-size:12px;color:{COLORS["text_secondary"]};margin-left:8px;">{member["role"]}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

            section_header("FUNDING", margin_top=18 if co.get("team") else 0)
            funding_str = co.get("funding_total") or "Undisclosed"
            st.markdown(
                f'<div style="font-size:20px;font-weight:800;color:{COLORS["text"]};margin-bottom:8px;">'
                f'{funding_str}</div>',
                unsafe_allow_html=True,
            )

            if co.get("co_investors"):
                section_header("CO-INVESTORS")
                for inv in co["co_investors"]:
                    st.markdown(
                        f'<div style="font-size:12px;color:#374151;margin-bottom:4px;">· {inv}</div>',
                        unsafe_allow_html=True,
                    )

        if co.get("primary_category"):
            df_pipe = load_companies()
            rel = df_pipe[df_pipe["primary_category"] == co["primary_category"]].head(8)
            if not rel.empty:
                st.markdown(
                    f'<hr style="border:none;border-top:1px solid {COLORS["border_light"]};margin:24px 0 0;">',
                    unsafe_allow_html=True,
                )
                section_header(f'RELATED PIPELINE — {co["primary_category"].upper()}', margin_top=12)
                st.markdown(
                    f'<div style="font-size:12px;color:{COLORS["text_secondary"]};margin-bottom:12px;">'
                    f'Companies in our sourcing database in the same category as {co["name"]}.</div>',
                    unsafe_allow_html=True,
                )
                import pandas as pd
                rel_cols = st.columns(4)
                for i, (_, r) in enumerate(rel.iterrows()):
                    with rel_cols[i % 4]:
                        score_str = f'{float(r["total_score"]):.2f}' if pd.notna(r.get("total_score")) else "—"
                        tier_str  = f'T{int(r["tier"])}' if pd.notna(r.get("tier")) else "—"
                        if st.button(r["name"], key=f"rel_pipe_{r['id']}_{sel_id}", use_container_width=True):
                            st.session_state.selected_company_id = int(r["id"])
                            st.session_state.page = "detail"
                            st.session_state.nav_origin = "portfolio"
                            st.rerun()
                        st.markdown(
                            f'<div style="font-size:10px;color:{COLORS["text_secondary"]};margin:-6px 0 8px;">'
                            f'{score_str} · {tier_str}</div>',
                            unsafe_allow_html=True,
                        )
        return

    # ── GRID VIEW ──────────────────────────────────────────────────
    n_cats = len({c.get("primary_category") for c in companies if c.get("primary_category")})
    page_title(
        "PORTFOLIO",
        f"{len(companies)} portfolio companies · {n_cats} categories",
    )

    n_cols = 3
    for row_start in range(0, len(companies), n_cols):
        chunk = companies[row_start : row_start + n_cols]
        cols  = st.columns(n_cols)
        for col, co in zip(cols, chunk):
            with col:
                stealth_html = stealth_badge() if co.get("is_stealth") else ""
                tag_html = ""
                if co.get("category_tag"):
                    for tag in co["category_tag"].split("·"):
                        tag_html += category_badge(tag.strip()) + "&nbsp;"
                loc_str    = co.get("location") or ""
                fund_str   = co.get("funding_total") or ""
                meta_parts = [p for p in [loc_str, fund_str] if p]
                meta_html  = (
                    f'<div style="font-size:10px;color:{COLORS["text_secondary"]};margin-top:8px;">'
                    f'{" · ".join(meta_parts)}</div>'
                ) if meta_parts else ""

                st.markdown(
                    f'<div class="ua-card" style="background:{COLORS["card_bg"]};'
                    f'border:1px solid {COLORS["card_border"]};'
                    f'padding:16px;margin-bottom:0;min-height:130px;">'
                    f'<div style="display:flex;justify-content:space-between;align-items:flex-start;">'
                    f'<div style="font-size:12px;font-weight:800;letter-spacing:0.05em;'
                    f'text-transform:uppercase;color:{COLORS["text"]};line-height:1.3;">{co["name"]}</div>'
                    f'{stealth_html}</div>'
                    f'<div style="margin-top:8px;">{tag_html}</div>'
                    f'<div style="font-size:12px;color:#374151;margin-top:8px;line-height:1.5;">'
                    f'{co.get("description","")}</div>'
                    f'{meta_html}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                if st.button("VIEW →", key=f"port_card_{co['id']}", use_container_width=True):
                    st.session_state.portfolio_selected_id = co["id"]
                    st.rerun()
                st.markdown("<br>", unsafe_allow_html=True)
