import streamlit as st

COLORS = {
    "bg":               "#ffffff",
    "text":             "#111111",
    "text_secondary":   "#6b7280",
    "text_muted":       "#9ca3af",
    "card_bg":          "#f2f2f2",
    "card_bg_light":    "#f9fafb",
    "card_border":      "#222222",
    "border_light":     "#e5e7eb",
    "border_mid":       "#d1d5db",
    "tier1":            "#111111",
    "tier2":            "#374151",
    "tier3":            "#9ca3af",
    "tier4":            "#e5e7eb",
    "gap_opportunity":  "#d97706",
    "gap_open":         "#dc2626",
    "gap_covered":      "#16a34a",
    "ukraine_blue":     "#3b82f6",
    "occupied_red":     "#dc2626",
    "occupied_yellow":  "#d97706",
    "chart_drone":      "#111111",
    "chart_ew":         "#dc2626",
    "chart_cyber":      "#059669",
    "chart_autonomy":   "#2563eb",
    "chart_cuas":       "#d97706",
    "chart_robotics":   "#7c3aed",
    "chart_c4isr":      "#6b7280",
}

TYPOGRAPHY = {
    "xs":   "10px",
    "sm":   "12px",
    "base": "14px",
    "md":   "16px",
    "lg":   "20px",
    "xl":   "26px",
    "xxl":  "32px",
}

SPACING = {
    "xs":  "4px",
    "sm":  "8px",
    "md":  "12px",
    "lg":  "16px",
    "xl":  "20px",
    "xxl": "24px",
    "xxxl":"32px",
}

CSS_GLOBAL = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');

.stApp {
    background-color: #ffffff !important;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}
[data-testid="stSidebar"] {
    background-color: #f7f7f7 !important;
    border-right: 1px solid #e5e7eb;
}
[data-testid="stSidebar"] > div:first-child { padding-top: 0 !important; }
section[data-testid="stSidebarContent"] { padding-top: 0 !important; }

h1, h2, h3, h4 {
    font-family: 'Inter', sans-serif !important;
    font-weight: 800 !important;
    letter-spacing: 0.05em !important;
    text-transform: uppercase !important;
    color: #111111 !important;
}
.stButton > button {
    background-color: #111111 !important;
    color: #ffffff !important;
    border: none !important;
    font-weight: 700 !important;
    font-size: 12px !important;
    letter-spacing: 0.08em !important;
    text-transform: uppercase !important;
    border-radius: 0 !important;
    padding: 8px 16px !important;
}
.stButton > button:hover {
    background-color: #374151 !important;
    border: none !important;
}
[data-testid="stSidebar"] .stButton > button {
    background-color: transparent !important;
    color: #111111 !important;
    border: 1px solid #d1d5db !important;
    text-align: left !important;
    justify-content: flex-start !important;
}
[data-testid="stSidebar"] .stButton > button:hover {
    background-color: #e5e7eb !important;
}
.stSelectbox > label, .stTextArea > label, .stTextInput > label {
    font-size: 10px !important;
    font-weight: 700 !important;
    letter-spacing: 0.08em !important;
    text-transform: uppercase !important;
    color: #111111 !important;
}
[data-testid="stSelectbox"] label,
[data-testid="stSelectbox"] label p,
[data-testid="stMultiSelect"] label,
[data-testid="stMultiSelect"] label p,
[data-testid="stTextInput"] label,
[data-testid="stTextInput"] label p,
[data-testid="stTextArea"] label,
[data-testid="stTextArea"] label p,
[data-testid="stDateInput"] label,
[data-testid="stDateInput"] label p,
[data-testid="stNumberInput"] label,
[data-testid="stNumberInput"] label p,
[data-testid="stTimeInput"] label,
[data-testid="stTimeInput"] label p {
    color: #111111 !important;
}
[data-testid="stRadio"] label,
[data-testid="stRadio"] label p,
[data-testid="stRadio"] [data-testid="stWidgetLabel"],
[data-testid="stRadio"] [data-testid="stWidgetLabel"] p {
    color: #111111 !important;
}
[data-testid="stExpander"] details summary,
[data-testid="stExpander"] details summary p,
[data-testid="stExpander"] summary,
[data-testid="stExpander"] summary p,
[data-testid="stExpander"] summary span {
    color: #111111 !important;
    font-weight: 600 !important;
}
[data-testid="stCheckbox"] label,
[data-testid="stCheckbox"] label p,
[data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p {
    color: #111111 !important;
}
[data-testid="stDataFrame"] { border: 1px solid #222222 !important; }
[data-testid="stDataFrame"] [role="columnheader"] {
    background-color: #111111 !important;
    color: #ffffff !important;
    font-size: 10px !important;
    font-weight: 700 !important;
    letter-spacing: 0.06em !important;
    text-transform: uppercase !important;
}
.stCheckbox > label {
    font-size: 12px !important;
    font-weight: 600 !important;
    color: #111111 !important;
}
[data-testid="stMetric"] {
    background: #f2f2f2;
    border: 1px solid #222222;
    padding: 16px;
    border-radius: 0 !important;
}
[data-testid="stMetricLabel"] p {
    font-size: 10px !important;
    font-weight: 700 !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    color: #6b7280 !important;
}
[data-testid="stMetricValue"] {
    font-size: 26px !important;
    font-weight: 800 !important;
    color: #111111 !important;
}
div[data-testid="stDownloadButton"] button {
    background-color: #374151 !important;
}
.stMarkdown, .stMarkdown p, .stMarkdown li,
.stMarkdown h1, .stMarkdown h2, .stMarkdown h3, .stMarkdown h4 {
    color: #111111 !important;
}
.ua-card {
    cursor: pointer;
    transition: transform 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
}
.ua-card:hover {
    transform: translateY(-2px);
    border-color: #111111 !important;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
}
/* Breadcrumb leading-segment: marker class on the preceding markdown div
   targets the next stButton via adjacent-sibling combinator, making it
   render as a small link rather than the default black button. */
div:has(> .ua-crumb-link) + div .stButton > button,
.ua-crumb-link ~ div .stButton > button {
    background-color: transparent !important;
    color: #6b7280 !important;
    border: none !important;
    padding: 4px 0 !important;
    font-size: 10px !important;
    font-weight: 700 !important;
    letter-spacing: 0.08em !important;
    text-decoration: none !important;
    text-align: left !important;
    justify-content: flex-start !important;
}
div:has(> .ua-crumb-link) + div .stButton > button:hover,
.ua-crumb-link ~ div .stButton > button:hover {
    background-color: transparent !important;
    color: #111111 !important;
    text-decoration: underline !important;
}
#MainMenu { visibility: hidden; }
footer    { visibility: hidden; }
header    { visibility: hidden; }
</style>
"""


def apply_theme() -> None:
    st.markdown(CSS_GLOBAL, unsafe_allow_html=True)
