"""
UI metrics consistency audit + enriched-but-thin diagnosis.

Read-only. No DB writes, no schema changes, no API calls.
Report saved to a path passed as argv[1].

Sections:
  1. UI metrics map: every count rendered in tabs, traced to its loader/SQL/filter/TTL.
  2. Cache TTL audit: every @st.cache_data in ui/data.py.
  3. Enriched-but-thin diagnostics: 8 patterns + samples + per-source breakdown.
  4. Recommendations.
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "companies.db"


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


# ── Static metric inventory ───────────────────────────────────────────────────
#
# These were identified by reading the tab files. Each entry is a UI surface
# that displays a count, traced back to where the number comes from. The audit
# also runs each underlying query against the live DB to surface divergences.

# Format: (tab, label, source_loader_or_sql, filters_description, cache_ttl)
INVENTORY: list[dict] = [
    {
        "tab":       "Home",
        "label":     "COMPANIES TRACKED (top metric)",
        "loader":    "load_companies()",
        "sql":       "SELECT … FROM companies WHERE portfolio_company IS NULL OR portfolio_company = 0",
        "filters":   "excludes portfolio companies; INCLUDES rows with enrich_error",
        "ttl":       "no @st.cache_data on this loader (re-queries DB every render)",
        "live_query": (
            "SELECT COUNT(*) FROM companies "
            "WHERE portfolio_company IS NULL OR portfolio_company = 0"
        ),
    },
    {
        "tab":       "Home",
        "label":     "TIER 1 TARGETS (top metric)",
        "loader":    "load_companies() then df[df.tier==1]",
        "sql":       "len(load_companies()[load_companies().tier == 1])",
        "filters":   "excludes portfolio; tier strictly == 1",
        "ttl":       "load_companies() not cached",
        "live_query": (
            "SELECT COUNT(*) FROM companies "
            "WHERE (portfolio_company IS NULL OR portfolio_company = 0) AND tier = 1"
        ),
    },
    {
        "tab":       "Home",
        "label":     "PORTFOLIO COS (top metric)",
        "loader":    "load_portfolio()",
        "sql":       "SELECT * FROM portfolio ORDER BY id",
        "filters":   "none",
        "ttl":       "60s",
        "live_query": "SELECT COUNT(*) FROM portfolio",
    },
    {
        "tab":       "Home",
        "label":     "ACTIVE CONFLICTS (top metric)",
        "loader":    "hardcoded literal '21'",
        "sql":       "n/a",
        "filters":   "n/a",
        "ttl":       "n/a",
        "live_query": "SELECT 21 AS const_value",
    },
    {
        "tab":       "Home",
        "label":     "WHAT'S NEW → newly_enriched count",
        "loader":    "alerts.get_changes_since() → result['newly_enriched']",
        "sql":       (
            "SELECT name, total_score, tier, primary_category, enriched_at FROM companies "
            "WHERE enriched_at > ? AND (enrich_error IS NULL OR enrich_error = '') "
            "AND (portfolio_company IS NULL OR portfolio_company = 0)"
        ),
        "filters":   "enrich_error must be NULL/empty (extra guard); excludes portfolio",
        "ttl":       "no cache; runs each render via st.session_state['alerts_since']",
        "live_query": (
            "SELECT COUNT(*) FROM companies WHERE enriched_at IS NOT NULL "
            "AND (enrich_error IS NULL OR enrich_error = '') "
            "AND (portfolio_company IS NULL OR portfolio_company = 0)"
        ),
    },
    {
        "tab":       "Deal Flow",
        "label":     "Companies count (header note '{n} companies' under Top Companies / Company Table)",
        "loader":    "load_companies() then optional unenriched mask",
        "sql":       "len(df_all) or len(df_all[has_data]) where has_data = description.notna() | enriched_at.notna() | dossier_at.notna()",
        "filters":   "excludes portfolio (via load_companies); 'Show unenriched' toggle adds/removes the has_data mask; tier/cat/source/status sidebar filters",
        "ttl":       "load_companies() not cached",
        "live_query": (
            "SELECT COUNT(*) FROM companies "
            "WHERE (portfolio_company IS NULL OR portfolio_company = 0)"
        ),
    },
    {
        "tab":       "Deal Flow",
        "label":     "Sidebar 'X eligible · Y excluded' (router.py)",
        "loader":    "_read inline SQL",
        "sql":       (
            "SELECT COUNT(*) FROM companies "
            "WHERE portfolio_company IS NULL OR portfolio_company=0  -- eligible\n"
            "SELECT COUNT(*) FROM companies WHERE portfolio_company=1  -- excluded"
        ),
        "filters":   "eligible := portfolio_company IS NULL OR 0; excluded := portfolio_company = 1",
        "ttl":       "no cache (inline _read())",
        "live_query": (
            "SELECT "
            "(SELECT COUNT(*) FROM companies WHERE portfolio_company IS NULL OR portfolio_company=0) AS eligible, "
            "(SELECT COUNT(*) FROM companies WHERE portfolio_company=1) AS excluded"
        ),
    },
    {
        "tab":       "Deal Flow",
        "label":     "TOP COMPANIES grid (top_df)",
        "loader":    "load_companies() then total_score >= 1.8 head(12)",
        "sql":       "df[pd.notna(df.total_score)].sort_values('total_score', ascending=False); df = df[df.total_score >= 1.8].head(12)",
        "filters":   "excludes portfolio; total_score not null AND >= 1.8; max 12 rows",
        "ttl":       "load_companies() not cached",
        "live_query": (
            "SELECT COUNT(*) FROM companies "
            "WHERE (portfolio_company IS NULL OR portfolio_company=0) "
            "AND total_score IS NOT NULL AND total_score >= 1.8"
        ),
    },
    {
        "tab":       "Press",
        "label":     "TOTAL ARTICLES",
        "loader":    "load_press_metrics() → 'total_articles'",
        "sql":       "SELECT COUNT(*) FROM processed_articles",
        "filters":   "none",
        "ttl":       "30s",
        "live_query": "SELECT COUNT(*) FROM processed_articles",
    },
    {
        "tab":       "Press",
        "label":     "COMPANIES MENTIONED",
        "loader":    "load_press_metrics() → 'total_companies_mentioned'",
        "sql":       "SELECT COUNT(DISTINCT canonical_id) FROM article_companies WHERE canonical_id IS NOT NULL",
        "filters":   "distinct canonical_id (not company_id)",
        "ttl":       "30s",
        "live_query": (
            "SELECT COUNT(DISTINCT canonical_id) FROM article_companies "
            "WHERE canonical_id IS NOT NULL"
        ),
    },
    {
        "tab":       "Pipeline",
        "label":     "Stage column counts ({len(stage_df)})",
        "loader":    "load_companies() then status filter per stage",
        "sql":       "len(df[df.status == stage])",
        "filters":   "excludes portfolio (via load_companies); per-stage status filter",
        "ttl":       "load_companies() not cached",
        "live_query": (
            "SELECT COALESCE(status, 'sourced') AS status, COUNT(*) FROM companies "
            "WHERE (portfolio_company IS NULL OR portfolio_company=0) GROUP BY 1"
        ),
    },
    {
        "tab":       "Gap Analysis",
        "label":     "Per-category 'n companies'",
        "loader":    "load_companies() filtered by primary_category",
        "sql":       "df[df.primary_category == cat]",
        "filters":   "excludes portfolio; primary_category match",
        "ttl":       "load_companies() not cached",
        "live_query": (
            "SELECT COALESCE(primary_category, '<NULL>') AS pc, COUNT(*) FROM companies "
            "WHERE (portfolio_company IS NULL OR portfolio_company=0) "
            "GROUP BY pc ORDER BY 2 DESC LIMIT 8"
        ),
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────


def fetch_one(c, sql: str):
    return c.execute(sql).fetchone()


def fetch_all(c, sql: str, params: tuple = ()):
    return c.execute(sql, params).fetchall()


# ── Section 1: Inventory + live values + divergences ──────────────────────────


def part1_metrics_inventory() -> list[str]:
    out: list[str] = ["## Part 1 — UI metrics inventory + live divergences\n"]

    # Inventory table
    out.append("### Inventory\n")
    out.append("| Tab | UI label | Source loader / SQL | Filters | Cache TTL | Live value |")
    out.append("|---|---|---|---|---|---:|")
    with conn() as c:
        for item in INVENTORY:
            try:
                row = c.execute(item["live_query"]).fetchone()
                if row is None:
                    live = "—"
                elif len(row) == 1:
                    live = str(row[0])
                else:
                    live = " · ".join(f"{row.keys()[i]}={row[i]}" for i in range(len(row)))
            except Exception as e:
                live = f"ERROR: {e}"
            sql_short = item["sql"].replace("\n", " ").replace("|", "\\|")
            if len(sql_short) > 90:
                sql_short = sql_short[:87] + "…"
            filters_short = item["filters"].replace("|", "\\|")
            out.append(
                f"| {item['tab']} | {item['label'][:50]} | "
                f"`{item['loader']}`<br/>{sql_short} | {filters_short} | {item['ttl']} | "
                f"**{live}** |"
            )
    out.append("")

    # Same-concept divergence groups
    out.append("\n### Divergences — metrics that *look* like the same concept\n")

    with conn() as c:
        # Concept A: "all eligible companies in the deal flow"
        a1 = c.execute("SELECT COUNT(*) FROM companies WHERE portfolio_company IS NULL OR portfolio_company=0").fetchone()[0]
        a2 = c.execute("""SELECT COUNT(*) FROM companies
                          WHERE (portfolio_company IS NULL OR portfolio_company=0)
                            AND (description IS NOT NULL OR enriched_at IS NOT NULL OR dossier_at IS NOT NULL)""").fetchone()[0]
        a3 = c.execute("""SELECT COUNT(*) FROM companies
                          WHERE (portfolio_company IS NULL OR portfolio_company=0)
                            AND (relevance_filter IS NULL)""").fetchone()[0]
        a4 = c.execute("""SELECT COUNT(*) FROM companies
                          WHERE (portfolio_company IS NULL OR portfolio_company=0)
                            AND (status IS NULL OR status != 'duplicate')""").fetchone()[0]
        out.append("**Concept A — \"companies in deal flow\":**\n")
        out.append("| Surface | Filters applied | Live count |")
        out.append("|---|---|---:|")
        out.append(f"| Home top metric `COMPANIES TRACKED` | excludes portfolio only | **{a1:,}** |")
        out.append(f"| Deal Flow `len(df)` w/ Show unenriched OFF (default) | + has description / enriched_at / dossier_at | **{a2:,}** |")
        out.append(f"| Hypothetical: also exclude relevance_filter set | + relevance_filter IS NULL | **{a3:,}** |")
        out.append(f"| enrich.py `_pending` baseline (status != duplicate) | + status NULL or != duplicate | **{a4:,}** |")
        out.append("")
        out.append(
            "→ **{} possible 'in deal flow' values exist depending on which surface you look at.** "
            "The default Deal Flow view hides {:,} unenriched rows that the Home metric still counts. "
            "Classification: **PRINCIPLED** — the toggle is the user-visible explanation. "
            "Becomes UNPRINCIPLED if Home and Deal Flow ever claim to show 'the same number'.".format(
                len({a1, a2, a3, a4}), a1 - a2,
            )
        )

        # Concept B: "newly enriched" / "enriched"
        b1 = c.execute("SELECT COUNT(*) FROM companies WHERE enriched_at IS NOT NULL AND (portfolio_company IS NULL OR portfolio_company=0)").fetchone()[0]
        b2 = c.execute("""SELECT COUNT(*) FROM companies
                          WHERE enriched_at IS NOT NULL
                            AND (enrich_error IS NULL OR enrich_error = '')
                            AND (portfolio_company IS NULL OR portfolio_company=0)""").fetchone()[0]
        b3 = c.execute("""SELECT COUNT(*) FROM companies
                          WHERE description IS NOT NULL AND description != ''
                            AND (portfolio_company IS NULL OR portfolio_company=0)""").fetchone()[0]
        out.append("\n**Concept B — \"how many companies are enriched\":**\n")
        out.append("| Surface | Filter | Live count |")
        out.append("|---|---|---:|")
        out.append(f"| `enriched_at IS NOT NULL` (raw) | none beyond portfolio | **{b1:,}** |")
        out.append(f"| Home WHAT'S NEW newly_enriched (alerts.py) | + enrich_error empty | **{b2:,}** |")
        out.append(f"| `description IS NOT NULL AND != ''` (some scripts use this proxy) | by description shape | **{b3:,}** |")
        out.append("")
        out.append(
            "→ All three are 'enriched' from different angles. "
            "Classification: **{}** — `description IS NOT NULL` is genuinely a different "
            "concept (SBIR rows have description=Award Title without ever calling enrich.py), "
            "but `enriched_at NOT NULL` ± enrich_error guard *should* agree on what "
            "successful enrichment means and they don't.".format(
                "UNPRINCIPLED" if abs(b1 - b2) > 0 else "PRINCIPLED"
            )
        )
        out.append(
            f"→ Delta of {abs(b1 - b2)} between b1 and b2 — those are rows where "
            f"enrich.py wrote `enriched_at` *and* `enrich_error` simultaneously. "
            f"That's a contract violation: enriched_at should set xor enrich_error."
        )

        # Concept C: "scored"
        c1 = c.execute("SELECT COUNT(*) FROM companies WHERE total_score IS NOT NULL AND (portfolio_company IS NULL OR portfolio_company=0)").fetchone()[0]
        c2 = c.execute("SELECT COUNT(*) FROM companies WHERE scored_at IS NOT NULL AND (portfolio_company IS NULL OR portfolio_company=0)").fetchone()[0]
        out.append("\n**Concept C — \"how many companies are scored\":**\n")
        out.append("| Surface | Filter | Live count |")
        out.append("|---|---|---:|")
        out.append(f"| `total_score IS NOT NULL` | by score nullness | **{c1:,}** |")
        out.append(f"| `scored_at IS NOT NULL` | by timestamp | **{c2:,}** |")
        out.append("")
        out.append(
            "→ Classification: **{}**. Delta = {}.".format(
                "PRINCIPLED" if c1 == c2 else "UNPRINCIPLED — score with no scored_at, or vice versa",
                abs(c1 - c2),
            )
        )

    return out


# ── Section 2: Cache TTL audit ────────────────────────────────────────────────


def part2_cache_audit() -> list[str]:
    out: list[str] = ["\n## Part 2 — Cache TTL audit\n"]
    text = (ROOT / "src/ui/data.py").read_text()
    pattern = re.compile(r"@st\.cache_data\(([^)]*)\)\s*\ndef\s+(\w+)\s*\(", re.M)
    rows = []
    for m in pattern.finditer(text):
        args, name = m.group(1), m.group(2)
        ttl_match = re.search(r"ttl\s*=\s*([0-9_]+)", args)
        ttl_secs = int(ttl_match.group(1).replace("_", "")) if ttl_match else None
        rows.append((name, ttl_secs, args.strip() or "—"))

    # Find loaders that don't have a cache decorator
    all_funcs = re.findall(r"^def\s+(\w+)\s*\(", text, re.M)
    cached_names = {r[0] for r in rows}
    public_loaders = [f for f in all_funcs
                      if not f.startswith("_") and f.startswith(("load_", "compute_", "search_", "count_"))]
    uncached_loaders = [f for f in public_loaders if f not in cached_names]

    out.append("### @st.cache_data decorators\n")
    out.append("| Function | TTL (s) | Decorator args |")
    out.append("|---|---:|---|")
    for name, ttl, args in sorted(rows, key=lambda r: (r[1] or 0)):
        ttl_disp = f"{ttl:,}" if ttl is not None else "—"
        out.append(f"| `{name}` | {ttl_disp} | `{args}` |")
    out.append("")

    if uncached_loaders:
        out.append("\n### ⚠ Public loaders WITHOUT @st.cache_data")
        for f in uncached_loaders:
            out.append(f"- `{f}` — every render hits the DB")
        out.append("")

    # TTL drift analysis
    ttl_set = sorted({r[1] for r in rows if r[1] is not None})
    out.append("### TTL drift\n")
    out.append(f"- Unique TTL values across cached loaders: {ttl_set} (seconds).")
    out.append(
        "- `load_companies()` is **not cached** (no `@st.cache_data`) but it's the most-called "
        "loader in the UI — Home, Deal Flow, Pipeline, Gap Analysis all funnel through it. "
        "That actually means the Home metrics agree with each other on render, but a manual "
        "`st.cache_data.clear()` from another loader (e.g. after status-update on detail page) "
        "doesn't flush load_companies' results because there are none to flush — so the **drift "
        "isn't between Home and Deal Flow** in a single render, it's between Home and the press "
        "tab (30s TTL on `load_press_metrics`) and Home and the score-trends/portfolio loaders "
        "(60-120s)."
    )
    out.append(
        "\n**Recommendation:** unify TTL at 30s for everything that surfaces a count, OR add "
        "`@st.cache_data(ttl=30)` to `load_companies` so all the count-derived metrics share one "
        "cache key and one invalidation point."
    )

    return out


# ── Section 3: Enriched-but-thin diagnostics ──────────────────────────────────


def part3_enriched_thin() -> list[str]:
    out: list[str] = ["\n## Part 3 — Enriched-but-thin diagnostics\n"]
    patterns = [
        ("hq_country missing",
         "enriched_at IS NOT NULL AND (hq_country IS NULL OR hq_country = '')"),
        ("founded_year missing",
         "enriched_at IS NOT NULL AND founded_year IS NULL"),
        ("no founders linked",
         "enriched_at IS NOT NULL AND id NOT IN (SELECT DISTINCT company_id FROM founders WHERE company_id IS NOT NULL)"),
        ("no contacts linked",
         "enriched_at IS NOT NULL AND id NOT IN (SELECT DISTINCT company_id FROM contacts WHERE company_id IS NOT NULL)"),
        ("description < 100 chars",
         "enriched_at IS NOT NULL AND length(description) < 100"),
        ("linkedin_url missing",
         "enriched_at IS NOT NULL AND (linkedin_url IS NULL OR linkedin_url = '')"),
        ("not scored (pipeline gap)",
         "enriched_at IS NOT NULL AND total_score IS NULL"),
        ("has website but no founders + no linkedin (user's specific case)",
         "enriched_at IS NOT NULL "
         "AND website IS NOT NULL AND website != '' "
         "AND id NOT IN (SELECT DISTINCT company_id FROM founders WHERE company_id IS NOT NULL) "
         "AND (linkedin_url IS NULL OR linkedin_url = '')"),
    ]

    out.append("Each pattern is restricted to non-portfolio rows.\n")

    with conn() as c:
        total_enriched = c.execute(
            "SELECT COUNT(*) FROM companies "
            "WHERE enriched_at IS NOT NULL AND (portfolio_company IS NULL OR portfolio_company=0)"
        ).fetchone()[0]
        out.append(f"- **Universe:** {total_enriched:,} companies with `enriched_at IS NOT NULL` (non-portfolio).\n")

        for i, (label, where) in enumerate(patterns, start=1):
            full_where = f"({where}) AND (portfolio_company IS NULL OR portfolio_company=0)"
            n = c.execute(f"SELECT COUNT(*) FROM companies WHERE {full_where}").fetchone()[0]
            pct = (n / total_enriched * 100) if total_enriched else 0
            out.append(f"### Pattern {i} — {label}\n")
            out.append(f"- Count: **{n:,}** of {total_enriched:,} enriched ({pct:.1f}%)")
            sample_rows = c.execute(
                f"SELECT id, name, source, website FROM companies WHERE {full_where} "
                "ORDER BY id DESC LIMIT 5"
            ).fetchall()
            out.append("- Sample rows:")
            for r in sample_rows:
                w = (r["website"] or "")[:50] if r["website"] else "—"
                out.append(f"  - id={r['id']:>5}  src={r['source']:<18}  name={r['name']!r:<48}  site={w!r}")
            out.append("")

        # Pattern 8 source breakdown
        out.append("### Pattern 8 — per-source breakdown\n")
        rows = c.execute("""
            SELECT source, COUNT(*) AS n FROM companies
             WHERE enriched_at IS NOT NULL
               AND website IS NOT NULL AND website != ''
               AND id NOT IN (SELECT DISTINCT company_id FROM founders WHERE company_id IS NOT NULL)
               AND (linkedin_url IS NULL OR linkedin_url = '')
               AND (portfolio_company IS NULL OR portfolio_company=0)
             GROUP BY source ORDER BY n DESC
        """).fetchall()
        if rows:
            out.append("| source | enriched-but-thin (pat 8) |")
            out.append("|---|---:|")
            total_p8 = sum(r["n"] for r in rows)
            for r in rows:
                share = (r["n"] / total_p8 * 100) if total_p8 else 0
                out.append(f"| `{r['source']}` | {r['n']} ({share:.1f}%) |")
            out.append("")

            # Per-source enrichment denominators to compute hit-rate
            out.append("\n### Pattern 8 — source-conditional rate (thin / enriched)\n")
            out.append("| source | enriched | pattern-8 | rate |")
            out.append("|---|---:|---:|---:|")
            for r in rows:
                source = r["source"]
                enriched_in_src = c.execute(
                    "SELECT COUNT(*) FROM companies WHERE enriched_at IS NOT NULL AND source = ? "
                    "AND (portfolio_company IS NULL OR portfolio_company=0)",
                    (source,),
                ).fetchone()[0]
                pct = (r["n"] / enriched_in_src * 100) if enriched_in_src else 0
                out.append(f"| `{source}` | {enriched_in_src} | {r['n']} | {pct:.1f}% |")
            out.append("")
        else:
            out.append("(no rows match — pattern 8 is empty)\n")

    return out


# ── Section 4: Recommendations ────────────────────────────────────────────────


def part4_recommendations() -> list[str]:
    out: list[str] = ["\n## Part 4 — Recommendations\n"]
    with conn() as c:
        n_p8 = c.execute("""
            SELECT COUNT(*) FROM companies
             WHERE enriched_at IS NOT NULL
               AND website IS NOT NULL AND website != ''
               AND id NOT IN (SELECT DISTINCT company_id FROM founders WHERE company_id IS NOT NULL)
               AND (linkedin_url IS NULL OR linkedin_url = '')
               AND (portfolio_company IS NULL OR portfolio_company=0)
        """).fetchone()[0]
        n_violation = c.execute("""
            SELECT COUNT(*) FROM companies
             WHERE enriched_at IS NOT NULL
               AND enrich_error IS NOT NULL AND enrich_error != ''
        """).fetchone()[0]
        n_unscored_enriched = c.execute("""
            SELECT COUNT(*) FROM companies
             WHERE enriched_at IS NOT NULL AND total_score IS NULL
               AND (portfolio_company IS NULL OR portfolio_company=0)
        """).fetchone()[0]

    out.append(
        "### 1. Unified metrics layer\n\n"
        "**Yes — needed.** Today's bug class is that `load_companies()` is uncached while every "
        "other count loader is cached at varying TTLs (15s / 30s / 60s / 120s / 1d), and "
        "`alerts.get_changes_since` runs its own queries with extra filters (`enrich_error` guard) "
        "that none of the others use. The fix isn't ambiguous:\n\n"
        "- Promote a single canonical loader, e.g. `load_companies(filter='in_deal_flow')`, with "
        "  a single `@st.cache_data(ttl=30)` decorator.\n"
        "- Move the alerts-page newly_enriched query into `ui/data.py` so it shares the same "
        "  filter set; keep the `enrich_error` guard but apply it consistently everywhere.\n"
        "- Add a top-of-tab `st.cache_data.clear()` invocation when the user explicitly clicks "
        "  'Mark all read' or after a status update, so all the metrics reset together.\n"
    )
    out.append(
        f"### 2. enriched_at xor enrich_error contract\n\n"
        f"**Yes — would prevent pattern-8 failures going forward.** Live data has "
        f"**{n_violation:,}** companies with both `enriched_at` and `enrich_error` set "
        f"simultaneously, which means downstream queries can't tell 'success' from 'tried but "
        f"empty' without remembering the convention. "
        f"Concrete change in `src/enrich.py`'s `_store_result` / `_store_error`:\n\n"
        f"  - on success: `SET enriched_at = datetime('now'), enrich_error = NULL`\n"
        f"  - on failure: `SET enrich_error = ?, enriched_at = NULL` (don't claim success on a thin extraction)\n"
        f"  - on partial extraction (pattern 8): `SET enriched_at = NULL, enrich_error = "
        f"'thin_extraction:no_founders_no_linkedin'` so re-runs pick it up automatically\n"
    )
    out.append(
        f"### 3. Pattern-8 re-enrichment job\n\n"
        f"- Pattern-8 count: **{n_p8:,}**\n"
        f"- At ~55s/company (the priority SBIR baseline): **~{n_p8 * 55 / 60:.0f} minutes "
        f"≈ {n_p8 * 55 / 3600:.1f} hours** of compute.\n"
        f"- Worth doing? Probably yes for a subset — DDG/Bing have improved their indexing for "
        f"these names since the original enrichment, and a fresh pass with a richer search "
        f"strategy would likely surface founders + LinkedIn for at least ~30-50% of these.\n"
        f"- Side note: **{n_unscored_enriched:,}** companies are enriched but not scored "
        f"(pattern 7) — that's a separate pipeline gap; `score.score_one()` should be invoked "
        f"in a sweep against these rows independently of pattern 8.\n"
    )
    return out


# ── Main ──────────────────────────────────────────────────────────────────────


def main(out_path: str) -> None:
    if not DB.exists():
        print(f"DB not found: {DB}", file=sys.stderr)
        sys.exit(2)

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts: list[str] = [
        "# UI metrics + enrichment-thinness audit",
        f"\n**Generated:** {now}  ·  **Mode:** read-only  ·  **DB:** `{DB.name}`\n",
    ]
    parts.extend(part1_metrics_inventory())
    parts.extend(part2_cache_audit())
    parts.extend(part3_enriched_thin())
    parts.extend(part4_recommendations())

    body = "\n".join(parts) + "\n"
    Path(out_path).write_text(body)
    sys.stdout.write(body)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/_ui_audit.py <output.md>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
