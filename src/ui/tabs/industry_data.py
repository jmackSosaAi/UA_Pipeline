import altair as alt
import pandas as pd
import streamlit as st

from ui.components import page_title
from ui.data import _IND_COLORS, _IND_DATA
from ui.theme import COLORS


def render(conn=None) -> None:
    page_title(
        "INDUSTRY DATA",
        "Global defense technology market sizing, growth trends, and investment data. "
        "Sources listed at bottom.",
    )

    _CATS_CFG = [
        {
            "key": "Military Drones/UAV",
            "mkt": 18.2, "cagr": "8.9%", "proj2030": "$30.9B",
            "context": (
                "The fastest-growing weapons category in modern warfare. Ukraine deploys 150,000+ "
                "drones monthly. Fixed-wing ISR platforms dominate revenue but FPV attack drones "
                "are the fastest-growing subcategory. 60% of all defense tech VC funding in 2025 "
                "went to drone startups."
            ),
            "sources_txt": "GM Insights, Fortune Business Insights, MarketsandMarkets, PitchBook",
        },
        {
            "key": "Electronic Warfare",
            "mkt": 17.5, "cagr": "5.2%", "proj2030": "$26B",
            "context": (
                "Russia-Ukraine has demonstrated EW as a decisive battlefield factor. Jamming, "
                "spoofing, and signal intelligence define the electromagnetic battlespace. "
                "AI-driven cognitive EW systems are the next frontier. The US alone allocated "
                "$3.17B across 45 EW programs."
            ),
            "sources_txt": "MarketsandMarkets, Fortune Business Insights, Market.us, Precedence Research",
        },
        {
            "key": "Cybersecurity (Defense)",
            "mkt": 31.0, "cagr": "12.5%", "proj2030": "$56B",
            "context": (
                "The largest category by market size. Cyber operations are integrated into every "
                "phase of modern warfare. Network-centric warfare, secure communications, and "
                "offensive cyber capabilities drive spending. Growing overlap with EW as "
                "cyber-electromagnetic operations converge."
            ),
            "sources_txt": "Fortune Business Insights, MarketsandMarkets",
        },
        {
            "key": "Autonomous Systems",
            "mkt": 24.0, "cagr": "15%", "proj2030": "$48B",
            "context": (
                "Includes autonomous navigation, AI decision-making, swarm coordination, and "
                "GPS-denied operation. The US DoD Replicator Initiative allocated $500M in FY2024 "
                "for autonomous systems. Autonomy is the software layer that makes every other "
                "hardware category more lethal."
            ),
            "sources_txt": "PitchBook, MarketsandMarkets, Defense News",
        },
        {
            "key": "Counter-Drone/C-UAS",
            "mkt": 6.6, "cagr": "25.1%", "proj2030": "$20.3B",
            "context": (
                "The fastest CAGR in defense — 25%+ annually. Every drone deployed creates demand "
                "for a counter-drone system. RF jamming, directed energy weapons, kinetic "
                "interceptors, and AI-enabled detection are all growing. Elbit secured a $60M "
                "C-UAS contract from a European NATO member in January 2025 alone."
            ),
            "sources_txt": "MarketsandMarkets, Precedence Research, Fortune Business Insights, Allied Market Research",
        },
        {
            "key": "Military Robotics",
            "mkt": 12.0, "cagr": "14%", "proj2030": "$24B",
            "context": (
                "Ground and maritime unmanned systems. UGVs for logistics, reconnaissance, and "
                "combat. USVs for naval patrol and mine warfare. PitchBook reports 139% surge in "
                "defense robotics VC funding in 2025. Forterra raised $238M Series C for "
                "autonomous military vehicles."
            ),
            "sources_txt": "PitchBook, MarketsandMarkets",
        },
        {
            "key": "C4ISR",
            "mkt": 45.0, "cagr": "5%", "proj2030": "$58B",
            "context": (
                "Command, Control, Communications, Computers, Intelligence, Surveillance, "
                "Reconnaissance. The largest defense technology category. Slower growth than "
                "emerging categories but foundational to all modern military operations. Tactical "
                "radios, battle management systems, satellite comms, and sensor fusion."
            ),
            "sources_txt": "MarketsandMarkets, Allied Market Research",
        },
    ]

    for _cat in _CATS_CFG:
        _key   = _cat["key"]
        _color = _IND_COLORS[_key]

        st.markdown(
            f'<div style="padding:16px 0 4px;border-top:2px solid {_color};margin-top:8px;">'
            f'<span style="font-size:12px;font-weight:900;letter-spacing:0.06em;color:{COLORS["text"]};">'
            f'{_key.upper()}</span></div>',
            unsafe_allow_html=True,
        )

        _cc1, _cc2 = st.columns([3, 2])

        with _cc1:
            _cat_df = pd.DataFrame([
                {"Year": yr, "Value": v} for yr, v in _IND_DATA[_key].items()
            ])
            _cat_line = (
                alt.Chart(_cat_df)
                .mark_line(
                    color=_color, strokeWidth=2.5,
                    strokeDash=[5, 5] if _key == "C4ISR" else [1, 0],
                )
                .encode(
                    x=alt.X("Year:O", title="YEAR",
                             axis=alt.Axis(labelAngle=0, labelFontSize=9)),
                    y=alt.Y("Value:Q", title="$ BILLIONS",
                             scale=alt.Scale(zero=False),
                             axis=alt.Axis(labelFontSize=9, titleFontSize=9)),
                    tooltip=["Year:O",
                             alt.Tooltip("Value:Q", title="$ Billions", format=".1f")],
                )
            )
            _cat_dot = (
                alt.Chart(_cat_df)
                .mark_point(color=_color, size=60, filled=True)
                .encode(
                    x=alt.X("Year:O"),
                    y=alt.Y("Value:Q"),
                    tooltip=["Year:O",
                             alt.Tooltip("Value:Q", title="$ Billions", format=".1f")],
                )
            )
            st.altair_chart(
                (_cat_line + _cat_dot)
                .properties(width="container", height=180, background="white")
                .configure_view(strokeWidth=0, fill="white")
                .configure_axis(grid=True, gridColor="#f3f4f6", gridOpacity=0.8, labelColor="#111111", titleColor="#111111"),
                use_container_width=True,
            )

        with _cc2:
            st.markdown(
                f'<div style="font-size:32px;font-weight:900;color:{_color};line-height:1.0;">'
                f'${_cat["mkt"]}B</div>'
                f'<div style="font-size:10px;color:{COLORS["text_muted"]};margin-bottom:12px;">2025 market size</div>',
                unsafe_allow_html=True,
            )
            _sc1, _sc2, _sc3 = st.columns(3)
            for _scol, _slbl, _sval in [
                (_sc1, "2025 SIZE",  f"${_cat['mkt']}B"),
                (_sc2, "CAGR",      _cat["cagr"]),
                (_sc3, "PROJ 2030", _cat["proj2030"]),
            ]:
                with _scol:
                    st.markdown(
                        f'<div style="padding:8px 8px;background:{COLORS["card_bg_light"]};'
                        f'border:1px solid {COLORS["border_light"]};">'
                        f'<div style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
                        f'color:{COLORS["text_muted"]};">{_slbl}</div>'
                        f'<div style="font-size:12px;font-weight:900;color:{COLORS["text"]};">{_sval}</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
            st.markdown(
                f'<div style="font-size:10px;color:#374151;line-height:1.65;margin-top:12px;">'
                f'{_cat["context"]}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div style="font-size:10px;color:{COLORS["text_muted"]};margin-top:8px;">'
                f'Sources: {_cat["sources_txt"]}</div>',
                unsafe_allow_html=True,
            )

    st.markdown(
        f'<div style="margin-top:32px;padding-top:16px;border-top:1px solid {COLORS["border_light"]};">'
        f'<div style="font-size:10px;font-weight:700;letter-spacing:0.12em;'
        f'color:{COLORS["text_muted"]};margin-bottom:12px;">SOURCES</div></div>',
        unsafe_allow_html=True,
    )
    for _lbl, _url in [
        ("SIPRI Military Expenditure Database",
         "https://www.sipri.org/databases/milex"),
        ("SIPRI Trends in World Military Expenditure 2025",
         "https://www.sipri.org/publications/2026/sipri-fact-sheets/trends-world-military-expenditure-2025"),
        ("GM Insights — Military Drone Market",
         "https://www.gminsights.com/industry-analysis/military-drone-market"),
        ("MarketsandMarkets — Military Drone",
         "https://www.marketsandmarkets.com/Market-Reports/military-drone-market-221577711.html"),
        ("Fortune Business Insights — Military Drone",
         "https://www.fortunebusinessinsights.com/military-drone-market-102181"),
        ("MarketsandMarkets — Electronic Warfare",
         "https://www.marketsandmarkets.com/Market-Reports/electronic-warfare-market-1301.html"),
        ("Fortune Business Insights — Electronic Warfare",
         "https://www.fortunebusinessinsights.com/electronic-warfare-market-103290"),
        ("Market.us — Electronic Warfare",
         "https://market.us/report/electronic-warfare-market/"),
        ("MarketsandMarkets — Anti-Drone",
         "https://www.marketsandmarkets.com/Market-Reports/anti-drone-market-177013645.html"),
        ("MarketsandMarkets — C-UAS Systems",
         "https://www.marketsandmarkets.com/PressReleases/counter-cuas-systems.asp"),
        ("PitchBook — The Iron Bubble (defense VC)",
         "https://pitchbook.com/news/articles/the-iron-bubble-why-defense-tech-might-not-be-overhyped"),
        ("PitchBook — Defense Robotics VC surge",
         "https://pitchbook.com/news/articles/drone-deals-fueled-vcs-139-surge-into-defense-robotics"),
        ("Defense News — Best funding year 2025",
         "https://www.defensenews.com/industry/2026/01/20/defense-tech-startups-had-their-best-funding-year-ever-in-2025/"),
        ("Al Jazeera — Rise of Global Militarisation",
         "https://www.aljazeera.com/news/2026/4/29/five-charts-that-show-the-rise-of-global-militarisation"),
        ("Dealbook of Ukraine 2026",
         "https://en.ain.ua/2026/01/26/dealbook-of-ukraine-2026/"),
        ("Brave1 Investment Highlights 2025",
         "https://thedefender.media/en/2025/12/brave1-investments-highlights-2025/"),
    ]:
        st.markdown(f"- [{_lbl}]({_url})")
