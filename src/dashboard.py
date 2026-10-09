"""
UA Pipeline — Streamlit Dashboard
Run: streamlit run src/dashboard.py  (from project root)
"""
import sys
from pathlib import Path

_SRC  = Path(__file__).parent
_ROOT = _SRC.parent
sys.path.insert(0, str(_SRC))

import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="UA PIPELINE",
    layout="wide",
    initial_sidebar_state="expanded",
)

from db.migrate import DEFAULT_DB_PATH, migrate as migrate_database

try:
    migrate_database(DEFAULT_DB_PATH)
    _DB_READY = True
    _DB_INIT_ERROR = None
except Exception as exc:
    _DB_READY = False
    _DB_INIT_ERROR = exc

if not _DB_READY:
    st.error("Database initialization failed.")
    st.caption(f"Active DB path: {DEFAULT_DB_PATH}")
    st.exception(_DB_INIT_ERROR)
    st.stop()

from ui.preflight import render_preflight
from ui.router import get_current_page, render_sidebar
from ui.tabs import (
    deal_flow,
    gap_analysis,
    home,
    industry_data,
    pipeline,
    portfolio,
    press,
)
from ui.theme import apply_theme

apply_theme()

# ── Route ─────────────────────────────────────────────────────────────────────
# Resolve the active page and scroll-to-top BEFORE any body renders.
# Placing this above render_sidebar() and the body dispatch ensures the
# scroll iframe mounts as the first DOM child of the new render pass, so
# it fires before late-loading components (charts, competitive landscape)
# have a chance to anchor scroll position.

page = get_current_page()
cid  = st.session_state.get("selected_company_id")
_route_key = f"{page}:{cid or ''}"
_prev_route = st.session_state.get("_prev_route")

if _prev_route != _route_key:
    st.session_state["_prev_route"] = _route_key
    components.html(
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

try:
    render_sidebar()
except Exception as exc:
    st.sidebar.error("Sidebar failed to load.")
    st.sidebar.caption(str(exc))
render_preflight(DEFAULT_DB_PATH)
st.sidebar.caption(f"DB: `{DEFAULT_DB_PATH}`")

if page == "home":
    home.render()
elif page == "portfolio":
    portfolio.render()
elif page == "industry":
    industry_data.render()
elif page == "overview":
    deal_flow.render()
elif page == "detail" and cid:
    deal_flow.render(company_id=int(cid))
elif page == "gap":
    gap_analysis.render()
elif page == "press":
    press.render()
elif page == "pipeline":
    pipeline.render()
else:
    home.render()
