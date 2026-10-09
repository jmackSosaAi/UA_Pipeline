import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from ui.alerts import get_changes_since, get_last_viewed, update_last_viewed
from ui.components import stat_html
from ui.data import (
    _IND_COLORS, _IND_DATA, STATUS_LABELS, _load_ukraine_geojson,
    load_companies, load_portfolio,
)
from ui.theme import COLORS


def _render_alerts() -> None:
    """Render the WHAT'S NEW panel at the top of Home."""
    # Initialise session-scoped baseline (fixed for the session so rerun doesn't shift the window)
    if "alerts_since" not in st.session_state:
        last = get_last_viewed()
        if last is None:
            # First ever visit — set baseline silently, show nothing
            update_last_viewed()
            st.session_state["alerts_since"] = None
        else:
            st.session_state["alerts_since"] = last

    since = st.session_state.get("alerts_since")
    if since is None:
        return  # first visit — skip panel

    changes = get_changes_since(since)
    counts = {k: len(v) for k, v in changes.items()}
    total  = counts["new_companies"] + counts["newly_enriched"] + counts["score_changes"] + counts["status_changes"] + counts["outreach_updates"]

    # Header row
    hdr_left, hdr_right = st.columns([5, 1])
    with hdr_left:
        since_label = since[:10]
        st.markdown(
            f'<div style="font-size:12px;font-weight:800;letter-spacing:0.09em;'
            f'text-transform:uppercase;padding-bottom:4px;">'
            f'WHAT\'S NEW'
            f'<span style="font-weight:400;color:{COLORS["text_muted"]};margin-left:12px;font-size:10px;">'
            f'since {since_label}</span></div>',
            unsafe_allow_html=True,
        )
    with hdr_right:
        if st.button("Mark all read", key="alerts_dismiss", use_container_width=True):
            update_last_viewed()
            del st.session_state["alerts_since"]
            st.rerun()

    if total == 0:
        st.markdown(
            f'<div style="font-size:12px;color:{COLORS["text_muted"]};padding:8px 0 16px;">'
            'Up to date — no changes since last visit.</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div style="margin:0 0 20px;border-top:1px solid {COLORS["border_light"]};"></div>',
            unsafe_allow_html=True,
        )
        return

    # Summary badge bar
    _BADGE_DEFS = [
        ("new_companies",   "NEW LEADS",      "#2563eb"),
        ("newly_enriched",  "ENRICHED",       "#059669"),
        ("score_changes",   "SCORED",         "#7c3aed"),
        ("new_tier1",       "TIER 1",         "#dc2626"),
        ("status_changes",  "PIPELINE MOVES", "#d97706"),
        ("outreach_updates","OUTREACH",       "#0891b2"),
    ]
    badge_html = '<div style="display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px;">'
    for key, label, color in _BADGE_DEFS:
        n = counts.get(key, 0)
        opacity = "1" if n > 0 else "0.3"
        badge_html += (
            f'<span style="background:{color};color:#fff;opacity:{opacity};'
            f'font-size:10px;font-weight:700;padding:4px 12px;border-radius:3px;'
            f'letter-spacing:0.05em;white-space:nowrap;">'
            f'{n} {label}</span>'
        )
    badge_html += '</div>'
    st.markdown(badge_html, unsafe_allow_html=True)

    # Detail expanders (only shown when non-empty)
    if counts["new_tier1"]:
        with st.expander(f"Tier 1 Movers ({counts['new_tier1']})"):
            for r in changes["new_tier1"]:
                score = f'{float(r["total_score"]):.2f}' if r.get("total_score") else "—"
                prev  = f' ← {float(r["prev_score"]):.2f}' if r.get("prev_score") else ""
                st.markdown(
                    f'<div style="font-size:12px;padding:4px 0;">'
                    f'<b>{r["name"]}</b>'
                    f'<span style="color:{COLORS["text_secondary"]};margin-left:8px;">'
                    f'{score}{prev} · {r.get("primary_category") or "—"}</span></div>',
                    unsafe_allow_html=True,
                )

    if counts["new_companies"]:
        with st.expander(f"New Leads Discovered ({counts['new_companies']})"):
            for r in changes["new_companies"]:
                st.markdown(
                    f'<div style="font-size:12px;padding:4px 0;">'
                    f'<b>{r["company_name"]}</b>'
                    f'<span style="color:{COLORS["text_muted"]};margin-left:8px;">{r["source"]} · {(r.get("discovered_at") or "")[:10]}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    if counts["newly_enriched"]:
        with st.expander(f"Newly Enriched ({counts['newly_enriched']})"):
            for r in changes["newly_enriched"]:
                score = f'{float(r["total_score"]):.2f}' if r.get("total_score") else "unscored"
                tier  = f'T{int(r["tier"])}' if r.get("tier") else "—"
                st.markdown(
                    f'<div style="font-size:12px;padding:4px 0;">'
                    f'<b>{r["name"]}</b>'
                    f'<span style="color:{COLORS["text_muted"]};margin-left:8px;">'
                    f'{score} · {tier} · {r.get("primary_category") or "—"}</span></div>',
                    unsafe_allow_html=True,
                )

    if counts["score_changes"]:
        with st.expander(f"Score Updates ({counts['score_changes']})"):
            for r in changes["score_changes"]:
                score = f'{float(r["total_score"]):.2f}' if r.get("total_score") else "—"
                prev  = r.get("prev_score")
                if prev is not None:
                    arrow = "↑" if float(r["total_score"] or 0) > float(prev) else ("↓" if float(r["total_score"] or 0) < float(prev) else "→")
                    delta = f' {arrow} {float(prev):.2f}→{score}'
                else:
                    delta = f' = {score}'
                tier = f'T{int(r["tier"])}' if r.get("tier") else "—"
                st.markdown(
                    f'<div style="font-size:12px;padding:4px 0;">'
                    f'<b>{r["name"]}</b>'
                    f'<span style="color:{COLORS["text_muted"]};margin-left:8px;">{delta} · {tier}</span></div>',
                    unsafe_allow_html=True,
                )

    if counts["status_changes"]:
        with st.expander(f"Pipeline Moves ({counts['status_changes']})"):
            for r in changes["status_changes"]:
                label = STATUS_LABELS.get(r.get("status", ""), r.get("status", "—"))
                st.markdown(
                    f'<div style="font-size:12px;padding:4px 0;">'
                    f'<b>{r["name"]}</b>'
                    f'<span style="color:{COLORS["text_muted"]};margin-left:8px;">→ {label}</span></div>',
                    unsafe_allow_html=True,
                )

    if counts["outreach_updates"]:
        with st.expander(f"Outreach Activity ({counts['outreach_updates']})"):
            for r in changes["outreach_updates"]:
                st.markdown(
                    f'<div style="font-size:12px;padding:4px 0;">'
                    f'<b>{r["name"]}</b>'
                    f'<span style="color:{COLORS["text_muted"]};margin-left:8px;">'
                    f'{r.get("outreach_status") or "—"} · {r.get("outreach_date_last_contact") or "—"}'
                    f'</span></div>',
                    unsafe_allow_html=True,
                )

    st.markdown(
        f'<div style="margin:16px 0 20px;border-top:1px solid {COLORS["border_light"]};"></div>',
        unsafe_allow_html=True,
    )


def render(conn=None) -> None:
    import folium
    from streamlit_folium import st_folium

    _render_alerts()

    # ── ROW 1: Metrics strip ──────────────────────────────────────────────────
    _co = load_companies()
    _pf = load_portfolio()
    _t1 = int((_co["tier"] == 1).sum()) if not _co.empty else 0
    _pf_n = len(_pf) if _pf is not None else 0

    _mc = st.columns(4)
    for _col, _lbl, _val in [
        (_mc[0], "COMPANIES TRACKED", str(len(_co))),
        (_mc[1], "TIER 1 TARGETS",    str(_t1)),
        (_mc[2], "PORTFOLIO COS",     str(_pf_n)),
        (_mc[3], "ACTIVE CONFLICTS",  "21"),
    ]:
        with _col:
            st.markdown(stat_html(_lbl, _val), unsafe_allow_html=True)

    st.markdown(
        f'<div style="margin:20px 0 0;border-top:1px solid {COLORS["border_light"]};padding-top:16px;"></div>',
        unsafe_allow_html=True,
    )

    # ── ROW 2: Ukraine conflict map (60%) │ Globe (40%) ──────────────────────
    _c_map, _c_globe = st.columns([3, 2])

    with _c_map:
        st.markdown(
            '<div style="font-size:10px;font-weight:700;letter-spacing:0.14em;'
            f'color:{COLORS["text_secondary"]};margin-bottom:8px;">UKRAINE CONFLICT MAP</div>',
            unsafe_allow_html=True,
        )

        def _occ_style(feature):
            _nm = (
                feature.get("properties", {}).get("name") or
                feature.get("properties", {}).get("NAME_1") or
                feature.get("properties", {}).get("NAME") or ""
            ).lower().replace("'", "").replace("’", "")
            if any(k in _nm for k in ("crym", "krim", "crimea", "sevastopol")):
                return {"fillColor": "#d97706", "fillOpacity": 0.40, "color": "#b45309", "weight": 0.8}
            if any(k in _nm for k in ("luhansk", "luhans")):
                return {"fillColor": "#d97706", "fillOpacity": 0.40, "color": "#b45309", "weight": 0.8}
            if any(k in _nm for k in ("donets",)):
                return {"fillColor": "#dc2626", "fillOpacity": 0.40, "color": "#991b1b", "weight": 0.8}
            if "zapori" in _nm:
                return {"fillColor": "#dc2626", "fillOpacity": 0.40, "color": "#991b1b", "weight": 0.8}
            if "kherson" in _nm:
                return {"fillColor": "#dc2626", "fillOpacity": 0.40, "color": "#991b1b", "weight": 0.8}
            return {"fillColor": "#3b82f6", "fillOpacity": 0.0, "color": "#93c5fd", "weight": 0.3}

        _gjson = _load_ukraine_geojson()

        m = folium.Map(
            location=[48.5, 32.0],
            zoom_start=6,
            tiles="CartoDB positron",
            min_zoom=5,
            max_zoom=10,
        )
        m.fit_bounds([[44.5, 22.5], [52.5, 40.5]])

        m.get_root().html.add_child(folium.Element(
            "<script>document.addEventListener('DOMContentLoaded',function(){"
            "Object.values(window).forEach(function(v){"
            "if(v&&typeof v.setMaxBounds==='function'&&!v._uabl){"
            "v.setMaxBounds([[44.0,22.0],[53.0,41.0]]);"
            "v.setMinZoom(5);v._uabl=true;}});});</script>"
        ))

        _UKRAINE_BORDER = [
            [51.50,24.10],[51.96,27.40],[51.85,30.90],
            [52.40,33.50],[51.20,35.45],[50.10,38.50],
            [49.45,39.80],[48.10,40.20],
            [47.10,38.00],[46.70,36.90],[45.82,37.20],
            [46.18,33.56],[46.36,32.00],[46.60,31.60],
            [46.15,30.70],[45.50,29.60],
            [48.25,26.40],[47.75,22.15],
            [48.40,22.20],[49.60,22.70],
            [50.30,23.60],[51.50,24.10],
        ]
        folium.Polygon(
            locations=_UKRAINE_BORDER,
            color="#3b82f6", weight=1.5,
            fill=True, fill_color="#3b82f6", fill_opacity=0.15,
            tooltip="Ukraine",
        ).add_to(m)

        if _gjson:
            _fp = _gjson.get("features", [{}])[0].get("properties", {})
            _nf = next((k for k in ("name", "NAME", "NAME_1", "region") if k in _fp), None)
            folium.GeoJson(
                _gjson,
                style_function=_occ_style,
                tooltip=folium.GeoJsonTooltip([_nf], labels=False) if _nf else None,
            ).add_to(m)
        else:
            folium.Polygon(
                locations=[
                    [46.12,33.56],[46.18,35.64],[45.37,36.66],[44.82,35.38],
                    [44.39,34.12],[44.50,33.00],[45.00,32.45],[45.80,33.00],[46.12,33.56],
                ],
                color="#b45309", weight=0.8, fill=True,
                fill_color="#d97706", fill_opacity=0.40,
                tooltip="Russian-occupied: Crimea",
            ).add_to(m)
            folium.Polygon(
                locations=[
                    [46.50,32.65],[46.75,33.60],[47.00,34.80],[47.12,35.60],
                    [47.30,36.20],[47.52,36.65],[47.85,36.90],[48.10,37.45],
                    [48.45,37.85],[48.80,38.20],[49.10,38.60],
                    [49.10,38.60],[48.80,38.20],[48.45,37.85],
                    [47.20,38.10],[46.82,37.22],[46.16,35.63],
                    [46.12,33.56],[46.50,32.65],
                ],
                color="#991b1b", weight=0.8, fill=True,
                fill_color="#dc2626", fill_opacity=0.40,
                tooltip="Contested/Frontline",
            ).add_to(m)
            folium.Polygon(
                locations=[
                    [48.45,37.85],[48.80,38.20],[49.10,38.60],[49.40,40.25],
                    [47.90,40.00],[47.20,38.10],[48.45,37.85],
                ],
                color="#b45309", weight=0.8, fill=True,
                fill_color="#d97706", fill_opacity=0.40,
                tooltip="Russian-occupied: Luhansk",
            ).add_to(m)

        _CITIES = [
            ("Kyiv",         50.45, 30.52, "ukraine"),
            ("Lviv",         49.84, 24.03, "ukraine"),
            ("Odesa",        46.48, 30.73, "ukraine"),
            ("Kharkiv",      49.99, 36.23, "ukraine"),
            ("Dnipro",       48.47, 35.04, "ukraine"),
            ("Zaporizhzhia", 47.84, 35.14, "ukraine"),
            ("Mykolaiv",     46.97, 31.99, "ukraine"),
            ("Kherson",      46.64, 32.62, "ukraine"),
            ("Chernihiv",    51.50, 31.29, "ukraine"),
            ("Sumy",         50.91, 34.80, "ukraine"),
            ("Donetsk",      48.00, 37.80, "occupied"),
            ("Luhansk",      48.57, 39.31, "occupied"),
            ("Mariupol",     47.10, 37.55, "occupied"),
            ("Melitopol",    46.85, 35.37, "occupied"),
            ("Simferopol",   44.95, 34.10, "occupied"),
        ]
        for _city, _lat, _lon, _side in _CITIES:
            _clr = "#111111" if _side == "ukraine" else "#cc1a1a"
            folium.Marker(
                location=[_lat, _lon],
                icon=folium.DivIcon(
                    html=(
                        f'<div style="font-size:10px;font-weight:800;color:{_clr};'
                        f'white-space:nowrap;'
                        f'text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff;">'
                        f'▪ {_city}</div>'
                    ),
                    icon_size=(90, 14), icon_anchor=(0, 7),
                ),
                tooltip=f"{_city} ({'Ukraine' if _side == 'ukraine' else 'Occupied'})",
            ).add_to(m)

        st_folium(m, width=None, height=420, returned_objects=[])

        st.markdown(
            f'<div style="font-size:10px;color:{COLORS["text_secondary"]};margin-top:8px;'
            'display:flex;gap:16px;flex-wrap:wrap;">'
            '<span><span style="color:#3b82f6;font-weight:900;">■</span> Ukraine-controlled</span>'
            '<span><span style="color:#dc2626;font-weight:900;">■</span> Contested/Frontline</span>'
            '<span><span style="color:#d97706;font-weight:900;">■</span> Russian-occupied</span>'
            '</div>',
            unsafe_allow_html=True,
        )

    with _c_globe:
        st.markdown(
            '<div style="font-size:10px;font-weight:700;letter-spacing:0.14em;'
            f'color:{COLORS["text_secondary"]};margin-bottom:8px;">GLOBAL CONFLICT LANDSCAPE</div>',
            unsafe_allow_html=True,
        )

        _globe_html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#ffffff; display:flex; justify-content:center; align-items:center; width:100%; height:420px; overflow:hidden; }
  canvas { display:block; }
</style>
</head>
<body>
<canvas id="globe" width="400" height="400"></canvas>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/topojson/3.0.2/topojson.min.js"></script>
<script>
(function() {
  var W = 400, H = 400;
  var canvas = document.getElementById('globe');
  var ctx = canvas.getContext('2d');

  var CONFLICT_NUMS = new Set([
    804,  // Ukraine
    643,  // Russia
    104,  // Myanmar
    729,  // Sudan
    231,  // Ethiopia
    706,  // Somalia
    760,  // Syria
    887,  // Yemen
    180,  // DR Congo
    466,  // Mali
    854,  // Burkina Faso
    562,  // Niger
    566,  // Nigeria
    376,  // Israel
    275,  // Palestine
    586,  // Pakistan
    4,    // Afghanistan
    170,  // Colombia
    332,  // Haiti
    434,  // Libya
    368,  // Iraq
  ]);

  var CONFLICT_LABELS = {
    804: "Ukraine",
    643: "Russia",
    376: "Israel",
    760: "Syria",
    887: "Yemen",
    368: "Iraq",
    4:   "Afghanistan",
    729: "Sudan",
    231: "Ethiopia",
    434: "Libya",
  };

  var projection = d3.geoOrthographic()
    .scale(190)
    .translate([W/2, H/2])
    .clipAngle(90)
    .rotate([-20, -25, 0]);

  var path = d3.geoPath(projection, ctx);
  var sphere = {type: "Sphere"};
  var rotation = [-20, -25, 0];
  var countries = null;
  var borders = null;
  var labelCentroids = {};

  function drawFrame() {
    ctx.clearRect(0, 0, W, H);

    ctx.beginPath();
    path(sphere);
    ctx.fillStyle = "#ffffff";
    ctx.fill();

    ctx.beginPath();
    path(sphere);
    ctx.strokeStyle = "#d1d5db";
    ctx.lineWidth = 1;
    ctx.stroke();

    if (!countries) return;

    countries.features.forEach(function(f) {
      var num = +f.id;
      ctx.beginPath();
      path(f);
      if (CONFLICT_NUMS.has(num)) {
        ctx.fillStyle = "#dc2626";
      } else {
        ctx.fillStyle = "#e5e7eb";
      }
      ctx.fill();
    });

    if (borders) {
      ctx.beginPath();
      path(borders);
      ctx.strokeStyle = "#9ca3af";
      ctx.lineWidth = 0.4;
      ctx.stroke();
    }

    ctx.font = "bold 8px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    Object.keys(CONFLICT_LABELS).forEach(function(num) {
      var centroid = labelCentroids[num];
      if (!centroid) return;
      var rot = projection.rotate();
      var visible = d3.geoDistance(centroid, [-rot[0], -rot[1]]) < Math.PI / 2;
      if (!visible) return;
      var pt = projection(centroid);
      if (!pt) return;
      ctx.fillStyle = "#ffffff";
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = "#ffffff";
      ctx.strokeText(CONFLICT_LABELS[num], pt[0], pt[1]);
      ctx.fillStyle = "#111111";
      ctx.fillText(CONFLICT_LABELS[num], pt[0], pt[1]);
    });
  }

  function animate() {
    rotation[0] += 0.3;
    projection.rotate(rotation);
    drawFrame();
    requestAnimationFrame(animate);
  }

  fetch("https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json")
    .then(function(r) { return r.json(); })
    .then(function(world) {
      countries = topojson.feature(world, world.objects.countries);
      borders = topojson.mesh(world, world.objects.countries, function(a, b) { return a !== b; });

      countries.features.forEach(function(f) {
        var num = +f.id;
        if (CONFLICT_LABELS[num] !== undefined) {
          var c = d3.geoCentroid(f);
          if (c) labelCentroids[num] = c;
        }
      });

      animate();
    })
    .catch(function(err) {
      ctx.fillStyle = "#6b7280";
      ctx.font = "13px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("Globe unavailable", W/2, H/2);
    });
})();
</script>
</body>
</html>"""

        components.html(_globe_html, height=420, scrolling=False)

        st.markdown(
            f'<div style="font-size:10px;font-style:italic;color:{COLORS["text_muted"]};'
            'margin-top:8px;text-align:center;">'
            'Ukraine represents the most technologically advanced theater of modern warfare.'
            '</div>',
            unsafe_allow_html=True,
        )

    st.markdown(
        f'<div style="margin:20px 0 0;border-top:1px solid {COLORS["border_light"]};padding-top:16px;"></div>',
        unsafe_allow_html=True,
    )

    # ── ROW 3: Defense industry growth (60%) │ Military expenditure (40%) ────
    _c_ind, _c_mil = st.columns([3, 2])

    with _c_ind:
        st.markdown(
            '<div style="font-size:10px;font-weight:700;letter-spacing:0.14em;'
            f'color:{COLORS["text_secondary"]};margin-bottom:8px;">DEFENSE INDUSTRY GROWTH BY CATEGORY</div>',
            unsafe_allow_html=True,
        )
        _ind_rows = [
            {"Category": cat, "Year": yr, "Value": v}
            for cat, yr_data in _IND_DATA.items()
            for yr, v in yr_data.items()
        ]
        _ind_df = pd.DataFrame(_ind_rows)
        _domain   = list(_IND_COLORS.keys())
        _clr_rng  = list(_IND_COLORS.values())
        _dash_rng = [[5, 5] if c == "C4ISR" else [1, 0] for c in _domain]

        _ind_lines = (
            alt.Chart(_ind_df).mark_line(strokeWidth=2.5).encode(
                x=alt.X("Year:O", title="YEAR",
                         axis=alt.Axis(labelAngle=0, labelFontSize=9, titleFontSize=9)),
                y=alt.Y("Value:Q", title="MARKET SIZE ($ BILLIONS)",
                         axis=alt.Axis(labelFontSize=9, titleFontSize=9)),
                color=alt.Color("Category:N",
                    scale=alt.Scale(domain=_domain, range=_clr_rng),
                    legend=alt.Legend(title=None, orient="right", labelFontSize=9, symbolSize=80),
                ),
                strokeDash=alt.StrokeDash("Category:N",
                    scale=alt.Scale(domain=_domain, range=_dash_rng), legend=None,
                ),
                tooltip=["Category:N", "Year:O",
                         alt.Tooltip("Value:Q", title="$ Billions", format=".1f")],
            )
        )
        _ind_dots = (
            alt.Chart(_ind_df).mark_point(size=35, filled=True).encode(
                x=alt.X("Year:O"),
                y=alt.Y("Value:Q"),
                color=alt.Color("Category:N",
                    scale=alt.Scale(domain=_domain, range=_clr_rng), legend=None,
                ),
                tooltip=["Category:N", "Year:O",
                         alt.Tooltip("Value:Q", title="$ Billions", format=".1f")],
            )
        )
        st.altair_chart(
            (_ind_lines + _ind_dots)
            .properties(width="container", height=320, background="white")
            .configure_view(strokeWidth=0, fill="white")
            .configure_axis(grid=True, gridColor="#f3f4f6", gridOpacity=0.8, labelColor="#111111", titleColor="#111111")
            .configure_legend(labelFontSize=9, symbolStrokeWidth=2.5, labelColor="#111111", titleColor="#111111"),
            use_container_width=True,
        )

    with _c_mil:
        st.markdown(
            '<div style="font-size:10px;font-weight:700;letter-spacing:0.14em;'
            f'color:{COLORS["text_secondary"]};margin-bottom:4px;">GLOBAL MILITARY EXPENDITURE (USD BILLIONS)</div>',
            unsafe_allow_html=True,
        )
        _milex_df = pd.DataFrame([
            {"year": 2016, "v": 2050}, {"year": 2017, "v": 2100},
            {"year": 2018, "v": 2180}, {"year": 2019, "v": 2240},
            {"year": 2020, "v": 2280}, {"year": 2021, "v": 2340},
            {"year": 2022, "v": 2443}, {"year": 2023, "v": 2500},
            {"year": 2024, "v": 2718}, {"year": 2025, "v": 2887},
        ])
        _milex_area = (
            alt.Chart(_milex_df)
            .mark_area(color="#d1d5db", opacity=0.9,
                       line={"color": "#111111", "strokeWidth": 1.5})
            .encode(
                x=alt.X("year:O", title=None,
                         axis=alt.Axis(labelAngle=0, labelFontSize=8)),
                y=alt.Y("v:Q", title=None, scale=alt.Scale(zero=False),
                         axis=alt.Axis(labelFontSize=8)),
                tooltip=["year:O", alt.Tooltip("v:Q", title="$ Billion")],
            )
        )
        _milex_lbl = (
            alt.Chart(pd.DataFrame([{"year": 2025, "v": 2887, "t": "$2.89T"}]))
            .mark_text(align="right", dx=-2, dy=-10, fontSize=9, fontWeight=700, color="#111")
            .encode(x="year:O", y="v:Q", text="t:N")
        )
        st.altair_chart(
            (_milex_area + _milex_lbl)
            .properties(width="container", height=150, background="white")
            .configure_view(strokeWidth=0, fill="white")
            .configure_axis(grid=False, labelColor="#111111", titleColor="#111111"),
            use_container_width=True,
        )

        st.markdown('<div style="margin-top:12px;"></div>', unsafe_allow_html=True)

        st.markdown(
            '<div style="font-size:10px;font-weight:700;letter-spacing:0.14em;'
            f'color:{COLORS["text_secondary"]};margin-bottom:4px;">UKRAINIAN DEFENSE TECH INVESTMENT ($M)</div>',
            unsafe_allow_html=True,
        )
        _ukr_df = pd.DataFrame([
            {"year": 2022, "v": 2}, {"year": 2023, "v": 5},
            {"year": 2024, "v": 40}, {"year": 2025, "v": 129},
        ])
        _ukr_area = (
            alt.Chart(_ukr_df)
            .mark_area(color="#d1d5db", opacity=0.9,
                       line={"color": "#111111", "strokeWidth": 1.5})
            .encode(
                x=alt.X("year:O", title=None,
                         axis=alt.Axis(labelAngle=0, labelFontSize=8)),
                y=alt.Y("v:Q", title=None, scale=alt.Scale(zero=True),
                         axis=alt.Axis(labelFontSize=8)),
                tooltip=["year:O", alt.Tooltip("v:Q", title="$M")],
            )
        )
        _ukr_lbl = (
            alt.Chart(pd.DataFrame([{"year": 2025, "v": 129, "t": "19× growth in 3 yrs"}]))
            .mark_text(align="right", dx=-2, dy=-10, fontSize=9, fontWeight=700, color="#111")
            .encode(x="year:O", y="v:Q", text="t:N")
        )
        st.altair_chart(
            (_ukr_area + _ukr_lbl)
            .properties(width="container", height=150, background="white")
            .configure_view(strokeWidth=0, fill="white")
            .configure_axis(grid=False, labelColor="#111111", titleColor="#111111"),
            use_container_width=True,
        )

    st.markdown(
        f'<div style="margin:20px 0 0;border-top:1px solid {COLORS["border_light"]};padding-top:16px;"></div>',
        unsafe_allow_html=True,
    )

    # ── ROW 4: Top military spenders 2025 (full width) ───────────────────────
    _spend_df = pd.DataFrame([
        {"country": "South Korea", "v": 48},
        {"country": "Japan",       "v": 58},
        {"country": "France",      "v": 68},
        {"country": "UK",          "v": 82},
        {"country": "Ukraine",     "v": 84},
        {"country": "India",       "v": 92},
        {"country": "Germany",     "v": 114},
        {"country": "Russia",      "v": 190},
        {"country": "China",       "v": 336},
        {"country": "USA",         "v": 954},
    ])
    _bars = (
        alt.Chart(_spend_df).mark_bar().encode(
            x=alt.X("v:Q", title="USD BILLIONS",
                     axis=alt.Axis(labelFontSize=9, titleFontSize=9)),
            y=alt.Y("country:N", sort=None, title=None,
                     axis=alt.Axis(labelFontSize=9)),
            color=alt.condition(
                alt.datum.country == "Ukraine",
                alt.value("#d97706"), alt.value("#374151"),
            ),
            tooltip=["country:N", alt.Tooltip("v:Q", title="$ Billions")],
        )
    )
    _bar_labels = (
        alt.Chart(_spend_df).mark_text(align="left", dx=3, fontSize=9, color="#374151").encode(
            x=alt.X("v:Q"), y=alt.Y("country:N", sort=None),
            text=alt.Text("v:Q", format=".0f"),
        )
    )
    st.altair_chart(
        (_bars + _bar_labels)
        .properties(
            width="container", height=280, background="white",
            title=alt.TitleParams(
                text="TOP MILITARY SPENDERS 2025",
                subtitle="Source: SIPRI, April 2026",
                fontSize=11, fontWeight=700, color="#111",
                subtitleFontSize=9, subtitleColor="#6b7280",
            ),
        )
        .configure_view(strokeWidth=0, fill="white")
        .configure_axis(grid=False, labelColor="#111111", titleColor="#111111"),
        use_container_width=True,
    )
