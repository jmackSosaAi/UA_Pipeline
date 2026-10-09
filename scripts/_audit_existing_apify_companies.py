"""Read-only audit — classify existing apify_leads companies against the
new BaselineFilter.

For each company that was inserted via the apify_leads collector before
the filter shipped, reconstruct the org bucket from
`raw_leads.source_metadata`, run BaselineFilter.evaluate, and write a
human-readable report to data/apify_baseline_backfill_report_YYYYMMDD.txt.

NO MUTATIONS: this script never writes to the DB. It exists so the
operator can decide, for the existing cohort, which orgs to keep and
which to retroactively reject. The decision-making is manual; this
script just surfaces "would-pass / would-reject-now" classifications.

Usage:
    python scripts/_audit_existing_apify_companies.py
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from collectors.apify_filter import BaselineFilter  # noqa: E402
from collectors.base import DB_PATH                  # noqa: E402
from collectors.dedup import normalize_name          # noqa: E402

REPORT_DIR = ROOT / "data"


def _load_apify_orgs(conn: sqlite3.Connection) -> list[dict]:
    """Pull every apify_leads raw_lead. companies-side lookup by name match."""
    rl_rows = conn.execute(
        """
        SELECT id, company_name, source_metadata, canonical_id
        FROM raw_leads
        WHERE source = 'apify_leads'
        ORDER BY id
        """,
    ).fetchall()

    # Build name→companies.id lookup once.
    name_to_company_id: dict[str, int] = {}
    for cid, cname in conn.execute("SELECT id, name FROM companies").fetchall():
        if cname:
            name_to_company_id.setdefault(normalize_name(cname), cid)

    out = []
    for r in rl_rows:
        cid = name_to_company_id.get(normalize_name(r["company_name"] or ""))
        out.append({
            "raw_lead_id":     r["id"],
            "company_name":    r["company_name"],
            "source_metadata": r["source_metadata"],
            "company_id":      cid,
        })
    return out


def _bucket_from_row(row: dict) -> dict:
    """Reconstruct the apify_leads bucket dict from raw_leads.source_metadata."""
    try:
        meta = json.loads(row["source_metadata"]) if row["source_metadata"] else {}
    except (TypeError, ValueError):
        meta = {}
    return {
        "org_name":    row["company_name"],
        "org_website": "",
        "org_meta":    meta,
        "leads":       [],
    }


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    bf = BaselineFilter.from_yaml()
    orgs = _load_apify_orgs(conn)

    if not orgs:
        print("no apify_leads rows found in raw_leads; nothing to audit.")
        return 0

    pass_lines: list[str] = []
    reject_lines: list[str] = []
    rule_failure_count: Counter[str] = Counter()

    for row in orgs:
        bucket = _bucket_from_row(row)
        result = bf.evaluate(bucket)
        meta = bucket["org_meta"]

        identity = (
            f"  raw_lead_id={row['raw_lead_id']:<5} "
            f"company_id={row['company_id'] or '-':<5} "
            f"name={bucket['org_name']!r}"
        )
        meta_summary = (
            f"    country={meta.get('country')!r} size={meta.get('size')!r} "
            f"founded_year={meta.get('founded_year')!r} "
            f"industry={meta.get('industry')!r}"
        )
        if result.passed:
            pass_lines.append(identity)
            pass_lines.append(meta_summary)
            pass_lines.append("")
        else:
            for rname in result.failed_rules:
                rule_failure_count[rname] += 1
            reject_lines.append(identity)
            reject_lines.append(meta_summary)
            reject_lines.append(f"    failed_rules: {result.failed_rules}")
            for r in result.reasons:
                reject_lines.append(f"      - {r}")
            reject_lines.append("")

    n_total  = len(orgs)
    n_pass   = len(pass_lines) and sum(1 for line in pass_lines if line.startswith("  raw_lead_id"))
    n_reject = sum(1 for line in reject_lines if line.startswith("  raw_lead_id"))

    stamp = _dt.datetime.now().strftime("%Y%m%d")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"apify_baseline_backfill_report_{stamp}.txt"

    header = [
        "=" * 78,
        f"Apify-baseline backfill audit  ({_dt.datetime.now().isoformat(timespec='seconds')})",
        "=" * 78,
        f"  total apify_leads rows:  {n_total}",
        f"  WOULD-PASS now:          {n_pass}",
        f"  WOULD-REJECT now:        {n_reject}",
        "",
        "  Rule-failure counts (orgs may fail multiple rules):",
    ]
    for rule, count in rule_failure_count.most_common():
        header.append(f"    {rule:<35} {count}")
    header += [
        "",
        "  NOTE: this report is read-only. Nothing in the DB was changed.",
        "  Decisions on whether to retroactively reject the WOULD-REJECT cohort",
        "  are manual — see docs/QUESTIONS_TO_ANSWER.md for the policy questions",
        "  whose answers will dictate that decision.",
        "=" * 78,
        "",
    ]

    body = (
        ["", "─" * 30, "WOULD-PASS", "─" * 30, ""] + pass_lines +
        ["", "─" * 30, "WOULD-REJECT", "─" * 30, ""] + reject_lines
    )

    out = "\n".join(header + body)
    report_path.write_text(out, encoding="utf-8")

    # Echo header to stdout so the operator sees the summary inline.
    print("\n".join(header))
    print(f"  report written to: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
