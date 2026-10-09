"""
Compact dashboard preflight checks.

This module is intentionally read-only: it opens the active SQLite database,
counts known tables, and reports environment key presence without printing
secret values.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import streamlit as st

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dashboard can still run without dotenv
    load_dotenv = None


COUNT_TABLES = [
    "companies",
    "raw_leads",
    "canonical_companies",
    "processed_articles",
    "article_companies",
    "sbir_awards",
    "portfolio",
]

API_KEYS = [
    "ANTHROPIC_API_KEY",
    "APIFY_TOKEN",
    "OPENAI_API_KEY",
    "BRAVE_API_KEY",
]


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _count_table(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def collect_preflight(db_path: str | Path) -> dict[str, Any]:
    """Return read-only dashboard preflight information."""
    active_db = Path(db_path)
    if load_dotenv is not None:
        load_dotenv()

    result: dict[str, Any] = {
        "db_path": str(active_db),
        "db_exists": active_db.exists(),
        "counts": {},
        "api_keys": {key: bool(os.environ.get(key)) for key in API_KEYS},
        "warnings": [],
        "error": None,
    }

    if not active_db.exists():
        result["warnings"].append("Active DB file is missing.")
        return result

    try:
        conn = _connect(active_db)
        for table in COUNT_TABLES:
            result["counts"][table] = _count_table(conn, table)

        if _table_exists(conn, "companies"):
            result["counts"]["enriched_companies"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM companies WHERE enriched_at IS NOT NULL"
                ).fetchone()[0]
            )
            result["counts"]["scored_companies"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM companies WHERE total_score IS NOT NULL"
                ).fetchone()[0]
            )
        else:
            result["counts"]["enriched_companies"] = None
            result["counts"]["scored_companies"] = None

        conn.close()
    except Exception as exc:
        result["error"] = str(exc)
        result["warnings"].append("Preflight DB checks failed.")
        return result

    missing = [name for name, count in result["counts"].items() if count is None]
    if missing:
        result["warnings"].append("Missing tables: " + ", ".join(missing))

    counts = result["counts"]
    if counts.get("companies") == 0:
        result["warnings"].append("companies is empty; Deal Flow will have no rows.")
    if counts.get("raw_leads") == 0:
        result["warnings"].append("raw_leads is empty; collectors have not produced leads.")
    if counts.get("canonical_companies") == 0:
        result["warnings"].append("canonical_companies is empty; promotion has no source set.")
    if counts.get("processed_articles") == 0 and counts.get("article_companies") == 0:
        result["warnings"].append("Press/article tables are empty.")

    return result


def render_preflight(db_path: str | Path) -> None:
    """Render a compact sidebar preflight panel."""
    info = collect_preflight(db_path)
    counts = info["counts"]

    with st.sidebar.expander("Preflight", expanded=False):
        st.caption(f"DB: `{info['db_path']}`")
        st.write(f"DB exists: `{bool(info['db_exists'])}`")

        if info["error"]:
            st.warning("Preflight DB checks failed.")
            st.caption(info["error"])

        st.markdown("**Counts**")
        for label in [
            "companies",
            "raw_leads",
            "canonical_companies",
            "enriched_companies",
            "scored_companies",
            "processed_articles",
            "article_companies",
            "sbir_awards",
            "portfolio",
        ]:
            val = counts.get(label)
            shown = "missing" if val is None else str(val)
            st.caption(f"{label}: `{shown}`")

        st.markdown("**API keys present**")
        for key, present in info["api_keys"].items():
            st.caption(f"{key}: `{present}`")

        if info["warnings"]:
            st.markdown("**Warnings**")
            for warning in info["warnings"]:
                st.warning(warning)
        else:
            st.success("Preflight checks passed.")
