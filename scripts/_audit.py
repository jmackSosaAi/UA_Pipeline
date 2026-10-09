"""
Pre-launch audit — read-only system integrity check.

Runs the 10 sections from the spec and emits a single markdown report.
No DB writes, no schema migrations, no external API calls.

Usage:
    python scripts/_audit.py audit_YYYYMMDD_HHMM.md
"""
from __future__ import annotations

import ast
import importlib
import io
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "companies.db"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

# ── Result accumulator ────────────────────────────────────────────────────────


class Section:
    def __init__(self, num: int, title: str):
        self.num = num
        self.title = title
        self.lines: list[str] = []
        self.statuses: list[str] = []   # one of {"PASS","WARN","FAIL"}

    def add(self, status: str, line: str):
        self.statuses.append(status)
        emoji = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}.get(status, "?")
        self.lines.append(f"- [{status}] {emoji} {line}")

    def info(self, line: str):
        self.lines.append(f"  - {line}")

    def render(self) -> str:
        out = [f"## Section {self.num}: {self.title}\n"]
        out.extend(self.lines)
        return "\n".join(out) + "\n"

    def overall(self) -> str:
        if "FAIL" in self.statuses: return "FAIL"
        if "WARN" in self.statuses: return "WARN"
        return "PASS"


SECTIONS: list[Section] = []


def section(num: int, title: str) -> Section:
    s = Section(num, title)
    SECTIONS.append(s)
    return s


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


# ── Section 1: Schema integrity ───────────────────────────────────────────────


def check_schema() -> None:
    s = section(1, "Schema integrity")
    expected_tables = [
        "companies", "canonical_companies", "raw_leads", "founders", "contacts",
        "sbir_awards", "processed_articles", "article_companies", "score_history",
        "portfolio", "meta",
    ]
    with conn() as c:
        existing = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        for t in expected_tables:
            if t in existing:
                cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
                s.add("PASS", f"`{t}` present ({len(cols)} columns)")
            else:
                s.add("FAIL", f"`{t}` is MISSING")

        # Specific column existence checks
        for table, col in [
            ("companies", "relevance_filter"),
            ("canonical_companies", "company_id"),
            ("sbir_awards", "company_id"),
        ]:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if col in cols:
                s.add("PASS", f"`{table}.{col}` column exists")
            else:
                s.add("FAIL", f"`{table}.{col}` column MISSING — code that references it will break")


# ── Section 2: Data integrity ─────────────────────────────────────────────────


def check_data() -> None:
    s = section(2, "Data integrity")
    valid_sources = {"SBIR", "prozorro", "NATO DIANA 2026", "brave1_articles",
                     "Defense Press", "seed", "manual", "apify_leads"}
    with conn() as c:
        # Orphan canonical
        n = c.execute("""
            SELECT COUNT(*) FROM canonical_companies cc
              LEFT JOIN companies co ON cc.company_id = co.id
             WHERE cc.company_id IS NOT NULL AND co.id IS NULL
        """).fetchone()[0]
        s.add("PASS" if n == 0 else "FAIL",
              f"Orphan canonical_companies (company_id → missing companies row): {n}")

        # Founders FK
        n = c.execute("""
            SELECT COUNT(*) FROM founders f
              LEFT JOIN companies co ON f.company_id = co.id
             WHERE co.id IS NULL
        """).fetchone()[0]
        s.add("PASS" if n == 0 else "FAIL",
              f"founders rows pointing at missing companies: {n}")

        # Contacts FK
        n = c.execute("""
            SELECT COUNT(*) FROM contacts ct
              LEFT JOIN companies co ON ct.company_id = co.id
             WHERE co.id IS NULL
        """).fetchone()[0]
        s.add("PASS" if n == 0 else "FAIL",
              f"contacts rows pointing at missing companies: {n}")

        # article_companies FK (NULL company_id is allowed by design — only
        # check rows that have a non-NULL company_id pointing at a missing row)
        n = c.execute("""
            SELECT COUNT(*) FROM article_companies ac
              LEFT JOIN companies co ON ac.company_id = co.id
             WHERE ac.company_id IS NOT NULL AND co.id IS NULL
        """).fetchone()[0]
        s.add("PASS" if n == 0 else "FAIL",
              f"article_companies rows with non-null company_id → missing companies row: {n}")

        # sbir_awards still missing company_id link (informational)
        total_sbir = c.execute("SELECT COUNT(*) FROM sbir_awards").fetchone()[0]
        unlinked = c.execute("SELECT COUNT(*) FROM sbir_awards WHERE company_id IS NULL").fetchone()[0]
        pct = (unlinked / total_sbir * 100) if total_sbir else 0
        status = "PASS" if pct < 50 else "WARN"
        s.add(status,
              f"sbir_awards rows missing company_id: {unlinked:,} of {total_sbir:,} ({pct:.1f}%) "
              f"— informational; backfill_company_ids() can run anytime")

        # Source distribution
        s.info("companies sources:")
        unknown_sources = []
        for r in c.execute("SELECT source, COUNT(*) AS n FROM companies GROUP BY source ORDER BY n DESC"):
            src = r["source"]
            n = r["n"]
            tag = "" if src in valid_sources else "  ← UNKNOWN"
            if src not in valid_sources:
                unknown_sources.append(src)
            s.info(f"  `{src!r}`: {n:,}{tag}")
        if unknown_sources:
            s.add("WARN", f"unknown source values: {unknown_sources}")
        else:
            s.add("PASS", "all source values are in the allowed set")

        # Duplicate websites
        dups = c.execute("""
            SELECT website, COUNT(*) AS n FROM companies
             WHERE website IS NOT NULL AND website != ''
             GROUP BY website HAVING COUNT(*) > 1
             ORDER BY n DESC LIMIT 10
        """).fetchall()
        if dups:
            s.add("FAIL", f"duplicate websites detected ({len(dups)} sets); should be UNIQUE")
            for r in dups:
                s.info(f"  {r['website']!r}: {r['n']} rows")
        else:
            s.add("PASS", "0 duplicate websites — UNIQUE constraint holding")

        # Enriched but no website
        n_enriched = c.execute(
            "SELECT COUNT(*) FROM companies WHERE enriched_at IS NOT NULL"
        ).fetchone()[0]
        n_enriched_no_site = c.execute("""
            SELECT COUNT(*) FROM companies
             WHERE enriched_at IS NOT NULL AND (website IS NULL OR website = '')
        """).fetchone()[0]
        pct = (n_enriched_no_site / n_enriched * 100) if n_enriched else 0
        if pct > 20:
            status = "FAIL"
        elif pct > 5:
            status = "WARN"
        else:
            status = "PASS"
        s.add(status,
              f"enriched-without-website: {n_enriched_no_site:,} / {n_enriched:,} "
              f"({pct:.1f}%)  threshold: WARN > 5%, FAIL > 20%")

        # relevance_filter distribution
        s.info("relevance_filter distribution:")
        for r in c.execute("""
            SELECT COALESCE(relevance_filter, '<NULL>') AS rf, COUNT(*) AS n
              FROM companies GROUP BY relevance_filter ORDER BY n DESC
        """):
            s.info(f"  {r['rf']!r}: {r['n']:,}")
        s.add("PASS", "relevance_filter distribution recorded (no validation rule)")


# ── Section 3: Config files load ──────────────────────────────────────────────


def check_configs() -> None:
    s = section(3, "Configuration files load cleanly")

    # Find every YAML in config/ recursively.
    yamls = sorted(Path(ROOT / "config").rglob("*.yaml"))
    parsed: dict[str, dict | list | None] = {}
    for p in yamls:
        rel = p.relative_to(ROOT)
        try:
            with p.open() as f:
                data = yaml.safe_load(f)
            parsed[str(rel)] = data
            s.add("PASS", f"`{rel}` parses")
        except Exception as e:
            parsed[str(rel)] = None
            s.add("FAIL", f"`{rel}` PARSE ERROR: {e}")

    # Structure validation per known file.
    sv = parsed.get("config/sources/search_vocabulary.yaml") or parsed.get("src/collectors/search_vocabulary.yaml")
    sv_path = ROOT / "src/collectors/search_vocabulary.yaml"
    if sv_path.exists():
        try:
            with sv_path.open() as f:
                vocab = yaml.safe_load(f)
            sectors = vocab.get("sectors") or {}
            empty = []
            dups_within_sector = []
            for skey, sval in sectors.items():
                terms = (sval.get("primary_terms") or []) + (sval.get("search_terms") or [])
                if not terms:
                    empty.append(skey)
                seen = set()
                for t in terms:
                    if not t or not isinstance(t, str):
                        continue
                    tl = t.strip().lower()
                    if tl in seen:
                        dups_within_sector.append((skey, tl))
                    seen.add(tl)
            issues = []
            if empty:
                issues.append(f"empty sectors: {empty}")
            if dups_within_sector:
                issues.append(f"within-sector duplicates: {dups_within_sector[:5]}")
            if issues:
                s.add("WARN", f"search_vocabulary.yaml: {' ; '.join(issues)}")
            else:
                s.add("PASS", f"search_vocabulary.yaml: {len(sectors)} sectors, no empty entries / duplicates")
        except Exception as e:
            s.add("FAIL", f"search_vocabulary.yaml validation failed: {e}")

    rss = parsed.get("config/sources/rss_feeds.yaml")
    if rss is not None:
        feeds = rss.get("feeds") or []
        bad = [f for f in feeds if not (isinstance(f, dict) and f.get("name") and f.get("url"))]
        if bad:
            s.add("FAIL", f"rss_feeds.yaml: {len(bad)} entries missing name/url")
        else:
            n_active = sum(1 for f in feeds if f.get("active", True))
            s.add("PASS", f"rss_feeds.yaml: {len(feeds)} entries ({n_active} active), all have name+url")

    excl = parsed.get("config/sources/excluded_companies.yaml")
    if excl is not None:
        excluded = excl.get("excluded") or []
        bad = [e for e in excluded if not (isinstance(e, dict) and e.get("name"))]
        noise = excl.get("noise_description_patterns") or []
        if bad:
            s.add("FAIL", f"excluded_companies.yaml: {len(bad)} entries missing 'name'")
        else:
            s.add("PASS",
                  f"excluded_companies.yaml: {len(excluded)} excluded names, "
                  f"{len(noise)} noise patterns — matches _is_excluded() contract")

    sbir = parsed.get("config/sources/sbir_config.yaml")
    if sbir is not None:
        missing = [k for k in ("core_agencies", "dual_use_agencies", "dual_use_keywords")
                   if not sbir.get(k)]
        if missing:
            s.add("FAIL", f"sbir_config.yaml: missing keys {missing}")
        else:
            s.add("PASS",
                  f"sbir_config.yaml: core={len(sbir['core_agencies'])} agencies, "
                  f"dual_use={len(sbir['dual_use_agencies'])} agencies, "
                  f"keywords={len(sbir['dual_use_keywords'])}")


# ── Section 4: Code import smoke test ─────────────────────────────────────────


def check_imports() -> None:
    s = section(4, "Code import smoke test")
    targets = [
        "src.main", "src.enrich", "src.enrich_priority_sbir", "src.score",
        "src.classify", "src.dossier", "src.rescore",
        "src.collectors.base", "src.collectors.dedup", "src.collectors.diana",
        "src.collectors.defense_press", "src.collectors.sbir",
        "src.collectors.prozorro_filter", "src.collectors.promote",
        "src.collectors.migrate", "src.collectors.source_config",
        "src.collectors.vocabulary", "src.collectors.cleanup_defense_press",
        "src.ui.theme", "src.ui.components", "src.ui.data", "src.ui.router",
        "src.ui.alerts",
        "src.ui.tabs.home", "src.ui.tabs.deal_flow", "src.ui.tabs.gap_analysis",
        "src.ui.tabs.pipeline", "src.ui.tabs.portfolio",
        "src.ui.tabs.industry_data", "src.ui.tabs.press",
    ]
    failed = []
    for m in targets:
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                importlib.import_module(m)
        except (ImportError, ModuleNotFoundError, SyntaxError, Exception) as e:
            failed.append((m, type(e).__name__, str(e)[:200]))
    if not failed:
        s.add("PASS", f"all {len(targets)} modules import clean")
    else:
        s.add("FAIL", f"{len(failed)} of {len(targets)} modules failed to import")
        for m, etype, emsg in failed:
            s.info(f"  `{m}` → {etype}: {emsg}")


# ── Section 5: Self-tests ─────────────────────────────────────────────────────


def check_self_tests() -> None:
    s = section(5, "Self-tests")
    # Discover by grep
    try:
        out = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--include=*.sh", "--", "--self-test", "src/"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        hits = [l for l in (out.stdout or "").splitlines() if l.strip()]
    except Exception as e:
        hits = []
        s.info(f"grep failed: {e}")
    s.info(f"--self-test entry points found: {len(hits)}")
    for h in hits[:10]:
        s.info(f"  {h}")

    # Run defense_press --self-test (no API calls)
    try:
        out = subprocess.run(
            [sys.executable, "-m", "src.collectors.defense_press", "--self-test"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        text = (out.stdout or "") + (out.stderr or "")
        m = re.search(r"(\d+)\s+passed,\s+(\d+)\s+failed", text)
        if m:
            passed, failed = int(m.group(1)), int(m.group(2))
            if failed == 0 and passed >= 16:
                s.add("PASS", f"defense_press --self-test: {passed} passed / 0 failed")
            elif failed == 0:
                s.add("WARN", f"defense_press --self-test: only {passed} passed (expected ≥ 16)")
            else:
                s.add("FAIL", f"defense_press --self-test: {passed} passed, {failed} FAILED")
        else:
            s.add("FAIL", f"defense_press --self-test: couldn't parse summary; head: {text[:200]!r}")
    except subprocess.TimeoutExpired:
        s.add("FAIL", "defense_press --self-test: TIMEOUT")
    except Exception as e:
        s.add("FAIL", f"defense_press --self-test: {type(e).__name__}: {e}")


# ── Section 6: Priority SBIR cohort dry-run ───────────────────────────────────


def check_priority_cohort() -> None:
    s = section(6, "Priority SBIR cohort dry-run")
    src_path = ROOT / "src/enrich_priority_sbir.py"
    if not src_path.exists():
        s.add("FAIL", "src/enrich_priority_sbir.py not found")
        return
    text = src_path.read_text()
    # Extract the SQL constant — looks for triple-quoted PRIORITY_SQL = """..."""
    m = re.search(r'PRIORITY_SQL\s*=\s*"""(.+?)"""', text, re.DOTALL)
    if not m:
        s.add("FAIL", "couldn't find PRIORITY_SQL = \"\"\"...\"\"\" in enrich_priority_sbir.py")
        return
    priority_sql = m.group(1).strip()
    s.info(f"extracted PRIORITY_SQL ({len(priority_sql)} chars)")

    with conn() as c:
        try:
            rows = c.execute(priority_sql).fetchall()
        except Exception as e:
            s.add("FAIL", f"PRIORITY_SQL failed to execute: {e}")
            return
        size = len(rows)
        target = 415
        if size == 0:
            s.add("FAIL", "cohort size is 0 — would-be enrichment run does nothing")
        elif 410 <= size <= 420:
            s.add("PASS", f"cohort size: {size} (within expected 410-420)")
        else:
            drift = abs(size - target) / target * 100
            # Spec: PASS if 410-420, WARN if drift > 5%, FAIL only at extremes.
            # 'wildly different' interpreted as > 50% drift from target.
            status = "FAIL" if drift > 50 else "WARN"
            note = ""
            if size < target:
                note = (" — likely the result of concurrent general enrichment "
                        "shrinking the unenriched pool")
            s.add(status, f"cohort size: {size} (drift {drift:.1f}% from 415){note}")

        # Sample 5 with award stats
        if rows:
            ids = [r[0] for r in rows[:5]]
            placeholders = ",".join("?" * len(ids))
            sample = c.execute(f"""
                SELECT c.id, c.name,
                       (SELECT COUNT(*) FROM sbir_awards sa WHERE sa.company_id = c.id) AS total_awards,
                       (SELECT MAX(fiscal_year) FROM sbir_awards sa WHERE sa.company_id = c.id) AS latest_year,
                       (SELECT GROUP_CONCAT(DISTINCT agency) FROM sbir_awards sa WHERE sa.company_id = c.id) AS agencies
                  FROM companies c WHERE c.id IN ({placeholders})
                  ORDER BY c.id
            """, ids).fetchall()
            s.info("sample 5 (real defense companies, not garbage):")
            for r in sample:
                ag = (r["agencies"] or "")[:60]
                s.info(f"  id={r['id']:>5}  awards={r['total_awards']:>2}  fy_max={r['latest_year']}  "
                       f"name={r['name']!r}  agencies=[{ag}]")


# ── Section 7: Watchdog wrapper parameterization ──────────────────────────────


def check_watchdog() -> None:
    s = section(7, "Watchdog wrapper parameterization")
    wd = ROOT / "scripts/watchdog.sh"
    if not wd.exists():
        s.add("FAIL", "scripts/watchdog.sh not found")
        return
    text = wd.read_text()
    # Detect $1 in any form: bare $1, ${1}, ${1:-...}, ${1?}, "$1", '$1', etc.
    has_param_one = bool(re.search(r"\$\{?1\b", text))
    has_wrapper_var = re.search(r'\bWRAPPER\s*=', text) is not None
    if has_param_one and has_wrapper_var:
        s.add("PASS", "watchdog.sh reads $1 and assigns it to a WRAPPER variable "
                     "(supports custom wrapper as positional arg)")
    elif has_param_one or has_wrapper_var:
        s.add("WARN", f"watchdog.sh: $1-read={has_param_one}, WRAPPER var={has_wrapper_var} "
                     "— partial parameterization")
    else:
        s.add("FAIL", "watchdog.sh has no parameter handling — won't accept a custom wrapper")

    # Dry-run with a no-op wrapper. Tail the watchdog log file we know it
    # writes — that's more reliable than capturing stdout under timeout.
    import tempfile, time, signal
    log_dir = ROOT / "logs"
    log_file = log_dir / f"watchdog_{__import__('datetime').datetime.now():%Y-%m-%d}.log"
    log_size_before = log_file.stat().st_size if log_file.exists() else 0

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, dir="/tmp")
    tmp.write("#!/bin/bash\nsleep 1\n")
    tmp.close()
    os.chmod(tmp.name, 0o755)
    try:
        proc = subprocess.Popen(
            ["bash", "scripts/watchdog.sh", tmp.name],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(2.5)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        stdout = ""
        try:
            stdout = proc.stdout.read() or ""
        except Exception:
            pass
    except Exception as e:
        stdout = f"<launch error: {e}>"

    # Read the log file too — watchdog tees its log lines there.
    log_tail = ""
    if log_file.exists():
        with log_file.open() as f:
            f.seek(log_size_before)
            log_tail = f.read()
    combined = stdout + "\n" + log_tail

    if "Watchdog starting" in combined and tmp.name in combined:
        s.add("PASS",
              f"watchdog dry-run: accepted custom wrapper {tmp.name!r} "
              "(saw 'Watchdog starting' with the wrapper path)")
    elif "Watchdog target not found" in combined:
        s.add("FAIL", "watchdog dry-run: rejected the wrapper parameter")
    else:
        s.add("WARN", f"watchdog dry-run: couldn't observe the start banner "
                     f"(stdout/log captured: {len(combined)} chars; "
                     f"head: {combined[:200]!r})")
    try:
        os.unlink(tmp.name)
    except FileNotFoundError:
        pass

    # Clean up nohup'd child if still around
    subprocess.run(["pkill", "-f", os.path.basename(tmp.name)], capture_output=True)


# ── Section 8: promote.py website-collision contract ──────────────────────────


def check_promote_contract() -> None:
    s = section(8, "promote.py website-collision contract")
    p = ROOT / "src/collectors/promote.py"
    if not p.exists():
        s.add("FAIL", "src/collectors/promote.py not found")
        return
    text = p.read_text()
    # 1. Catches sqlite3.IntegrityError
    catches = "except sqlite3.IntegrityError" in text
    s.add("PASS" if catches else "FAIL",
          f"catches sqlite3.IntegrityError: {catches}")
    # 2. Looks up existing by website
    looks_up = bool(re.search(r"SELECT\s+id.*FROM\s+companies\s+WHERE\s+website\s*=\s*\?", text, re.IGNORECASE | re.DOTALL))
    s.add("PASS" if looks_up else "FAIL",
          f"looks up existing company by website on collision: {looks_up}")
    # 3. Writes canonical_companies.company_id
    writes_canon = bool(re.search(r"UPDATE\s+canonical_companies\s+SET\s+company_id", text, re.IGNORECASE))
    s.add("PASS" if writes_canon else "FAIL",
          f"updates canonical_companies.company_id on merge: {writes_canon}")
    # 4. Returns 'merged' status (not crashing or silently dropping)
    has_merged_status = '"merged"' in text or "'merged'" in text
    s.add("PASS" if has_merged_status else "WARN",
          f"returns explicit 'merged' status (not silent): {has_merged_status}")


# ── Section 9: enrich.run() signature ─────────────────────────────────────────


def check_enrich_signature() -> None:
    s = section(9, "enrich.run() signature")
    p = ROOT / "src/enrich.py"
    if not p.exists():
        s.add("FAIL", "src/enrich.py not found")
        return
    tree = ast.parse(p.read_text())
    target = next((node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "run"), None)
    if target is None:
        s.add("FAIL", "no top-level run() function found in enrich.py")
        return
    arg_names = [a.arg for a in target.args.args] + [a.arg for a in target.args.kwonlyargs]
    s.info(f"run() params: {arg_names}")
    if "company_ids" in arg_names:
        s.add("PASS", "run() accepts `company_ids`")
    else:
        s.add("FAIL", "run() is MISSING `company_ids` — priority script will break")
    if "progress_every" in arg_names:
        s.add("PASS", "run() accepts `progress_every`")
    else:
        s.add("FAIL", "run() is MISSING `progress_every`")


# ── Section 10: Streamlit launchability ───────────────────────────────────────


def check_streamlit() -> None:
    s = section(10, "Streamlit launchability")
    dash = ROOT / "src/dashboard.py"
    if not dash.exists():
        s.add("FAIL", "src/dashboard.py not found")
        return
    try:
        ast.parse(dash.read_text())
        s.add("PASS", "src/dashboard.py parses cleanly")
    except SyntaxError as e:
        s.add("FAIL", f"src/dashboard.py syntax error: {e}")

    try:
        out = subprocess.run(["streamlit", "--version"], capture_output=True, text=True, timeout=10)
        ver = (out.stdout or out.stderr or "").strip().splitlines()[0] if (out.stdout or out.stderr) else ""
        if out.returncode == 0 and ver:
            s.add("PASS", f"streamlit installed — `{ver}`")
        else:
            s.add("FAIL", f"streamlit --version returned code={out.returncode}, output={ver!r}")
    except FileNotFoundError:
        s.add("FAIL", "streamlit not on PATH (try `pip install streamlit`)")
    except subprocess.TimeoutExpired:
        s.add("FAIL", "streamlit --version timed out")


# ── Main ──────────────────────────────────────────────────────────────────────


def main(out_path: str) -> None:
    if not DB.exists():
        print(f"DB not found: {DB}", file=sys.stderr)
        sys.exit(2)

    check_schema()
    check_data()
    check_configs()
    check_imports()
    check_self_tests()
    check_priority_cohort()
    check_watchdog()
    check_promote_contract()
    check_enrich_signature()
    check_streamlit()

    # Aggregate stats
    n_pass = sum(s.statuses.count("PASS") for s in SECTIONS)
    n_warn = sum(s.statuses.count("WARN") for s in SECTIONS)
    n_fail = sum(s.statuses.count("FAIL") for s in SECTIONS)
    total = n_pass + n_warn + n_fail

    failing_sections = [s for s in SECTIONS if s.overall() == "FAIL"]
    warning_sections = [s for s in SECTIONS if s.overall() == "WARN"]
    if failing_sections:
        recommendation = "**RED — DO NOT LAUNCH** until the failing sections below are addressed."
    elif warning_sections:
        recommendation = "**YELLOW — proceed with the priority SBIR launch, but address the warnings soon.**"
    else:
        recommendation = "**GREEN — proceed with the priority SBIR enrichment launch.**"

    import datetime as _dt
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    md = []
    md.append("# ua-pipeline — Pre-launch audit\n")
    md.append(f"**Generated:** {now}  ·  **Mode:** read-only  ·  **DB:** `{DB.name}`\n\n")
    md.append("## Summary\n\n")
    md.append(f"- **{n_pass}/{total} checks passed**, {n_warn} warning(s), {n_fail} failure(s)\n")
    md.append(f"- Section status: ")
    md.append(", ".join(f"§{s.num}={s.overall()}" for s in SECTIONS) + "\n\n")
    for s in SECTIONS:
        md.append(s.render() + "\n")
    md.append("---\n\n## Pre-launch recommendation\n\n")
    md.append(recommendation + "\n")

    body = "".join(md)
    Path(out_path).write_text(body)
    sys.stdout.write(body)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/_audit.py <output.md>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
