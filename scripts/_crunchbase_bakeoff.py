"""Crunchbase actor bake-off harness.

This is intentionally not a production collector. It samples companies,
preflights candidate actors, optionally runs surviving actors under a budget
cap, and writes evidence to docs/artifacts/crunchbase_bakeoff/.

Default mode is dry-run/preflight only: no paid actor calls.
Use --live to call actors.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import signal
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

load_dotenv(dotenv_path=ROOT / ".env")

DB_PATH = ROOT / "data" / "companies.db"
ARTIFACT_ROOT = ROOT / "docs" / "artifacts" / "crunchbase_bakeoff"
REPORT_PATH = ROOT / "docs" / "CRUNCHBASE_BAKEOFF.md"
FOLLOWUPS_PATH = ROOT / "docs" / "CRUNCHBASE_BAKEOFF_FOLLOWUPS.md"

SEED = 20260510
SAMPLE_PER_STRATUM = 10
DEFAULT_BUDGET_CAP_USD = 14.00
MAX_PRICE_PER_COMPANY_CALL_USD = 0.15
BASELINE_COHORT_SIZE = 972
METADATA_TIMEOUT_SECS = 20


@dataclass(frozen=True)
class ActorSpec:
    label: str
    actor_id: str


ACTORS = [
    ActorSpec("sovereigntaylor", "sovereigntaylor/crunchbase-scraper"),
    ActorSpec("parseforge", "parseforge/crunchbase-scraper"),
    ActorSpec("pratikdani", "pratikdani/crunchbase-companies-scraper"),
    ActorSpec("epctex", "epctex/crunchbase-scraper"),
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _eligible_sql(where: str) -> str:
    return f"""
        SELECT id, name, source, tier, website, linkedin_url
          FROM companies
         WHERE {where}
           AND name IS NOT NULL
           AND TRIM(name) != ''
           AND (
                (website IS NOT NULL AND TRIM(website) != '')
                OR (linkedin_url IS NOT NULL AND TRIM(linkedin_url) != '')
           )
         ORDER BY id
    """


def _load_rows(conn: sqlite3.Connection, where: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(_eligible_sql(where)).fetchall()]


def _pick(rows: list[dict[str, Any]], n: int, rng: random.Random, used: set[int]) -> list[dict[str, Any]]:
    available = [r for r in rows if int(r["id"]) not in used]
    rng.shuffle(available)
    picked = available[:n]
    used.update(int(r["id"]) for r in picked)
    return picked


def load_sample(seed: int = SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    used: set[int] = set()
    conn = _connect()
    try:
        strata = [
            ("tier_1_2", "tier IN (1, 2)"),
            ("tier_3", "tier = 3"),
            ("brave1_prozorro", "source IN ('brave1_articles', 'prozorro')"),
        ]
        sample: list[dict[str, Any]] = []
        for stratum, where in strata:
            rows = _load_rows(conn, where)
            if len(rows) < SAMPLE_PER_STRATUM:
                raise SystemExit(
                    f"ABORT: stratum {stratum!r} has only {len(rows)} eligible rows"
                )
            for row in _pick(rows, SAMPLE_PER_STRATUM, rng, used):
                row["stratum"] = stratum
                sample.append(row)
        return sample
    finally:
        conn.close()


def _safe_actor_label(actor_id: str) -> str:
    return actor_id.replace("/", "__")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def write_sample(sample: list[dict[str, Any]]) -> None:
    _write_json(ARTIFACT_ROOT / "sample.json", sample)


def _import_apify_client():
    from apify_client import ApifyClient

    return ApifyClient


class _MetadataTimeout(Exception):
    pass


def _metadata_alarm(_signum: int, _frame: Any) -> None:
    raise _MetadataTimeout(f"metadata lookup exceeded {METADATA_TIMEOUT_SECS}s")


def _actor_get_with_timeout(actor_client: Any) -> dict[str, Any] | None:
    old_handler = signal.signal(signal.SIGALRM, _metadata_alarm)
    signal.alarm(METADATA_TIMEOUT_SECS)
    try:
        return actor_client.get()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def _extract_schema(actor_meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not actor_meta:
        return None
    for key in ("inputSchema", "input_schema"):
        if isinstance(actor_meta.get(key), dict):
            return actor_meta[key]
    default_run_options = actor_meta.get("defaultRunOptions")
    if isinstance(default_run_options, dict):
        schema = default_run_options.get("inputSchema")
        if isinstance(schema, dict):
            return schema
    versions = actor_meta.get("versions")
    if isinstance(versions, list) and versions:
        schema = versions[0].get("inputSchema") if isinstance(versions[0], dict) else None
        if isinstance(schema, dict):
            return schema
    return None


def _schema_properties(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not schema:
        return {}
    props = schema.get("properties")
    return props if isinstance(props, dict) else {}


def _required_fields(schema: dict[str, Any] | None) -> set[str]:
    if not schema:
        return set()
    required = schema.get("required")
    return set(required) if isinstance(required, list) else set()


def _field_names(props: dict[str, Any]) -> set[str]:
    return {p.lower() for p in props}


def _input_acceptance(schema: dict[str, Any] | None) -> tuple[bool, str, list[str]]:
    props = _schema_properties(schema)
    names = _field_names(props)
    required = {f.lower() for f in _required_fields(schema)}
    if not props:
        return False, "input schema unavailable or has no properties", []

    name_like = {"name", "companyname", "company_name", "query", "search", "searchterm", "keyword", "keywords"}
    website_like = {"website", "url", "urls", "domain", "companyurl", "companyurls", "starturls", "starturls"}
    linkedin_like = {"linkedinurl", "linkedin_url", "linkedin", "profileurl", "profileurls"}
    crunchbase_only = {"crunchbaseurl", "crunchbase_url", "organizationurl", "organization_url"}

    has_name = bool(names & name_like)
    has_anchor = bool(names & (website_like | linkedin_like))
    has_generic_search = bool(names & {"query", "search", "searchterm", "keyword", "keywords"})
    required_cb = sorted(required & crunchbase_only)

    if required_cb and not (has_name or has_generic_search):
        return False, f"requires Crunchbase URL-like field(s): {', '.join(required_cb)}", sorted(props)
    if has_generic_search or (has_name and has_anchor):
        return True, "schema appears to accept name/search plus website/linkedin anchor", sorted(props)
    return False, "schema does not clearly accept name+website or name+linkedin_url", sorted(props)


def _flatten_example(value: Any) -> tuple[str, list[str]]:
    fields: list[str] = []
    chunks: list[str] = []

    def visit(item: Any, prefix: str = "") -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                fields.append(path)
                chunks.append(str(key))
                visit(child, path)
        elif isinstance(item, list):
            for child in item:
                visit(child, prefix)
        elif isinstance(item, str):
            chunks.append(item)
            stripped = item.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    visit(json.loads(stripped), prefix)
                except json.JSONDecodeError:
                    pass
        elif item is not None:
            chunks.append(str(item))

    visit(value)
    return " ".join(chunks).lower(), sorted(set(fields))


def _example_acceptance(example: Any) -> tuple[bool, str, list[str]]:
    if not example:
        return False, "exampleRunInput unavailable", []
    haystack, fields = _flatten_example(example)
    acceptable_terms = (
        "company",
        "companyname",
        "company_name",
        "name",
        "query",
        "search",
        "keyword",
        "website",
        "domain",
        "linkedin",
    )
    if any(term in haystack for term in acceptable_terms):
        return True, "exampleRunInput reveals acceptable company/search/website/linkedin input pattern", fields
    if "body" in fields and "json" in haystack:
        return True, "exampleRunInput exposes a generic JSON body; accepted for live bake-off validation", fields
    return False, "formal schema and exampleRunInput do not reveal acceptable input pattern", fields


def _pricing_summary(actor_meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not actor_meta:
        return []
    out = []
    for item in actor_meta.get("pricingInfos") or []:
        if not isinstance(item, dict):
            continue
        out.append({
            "pricingModel": item.get("pricingModel"),
            "pricePerUnitUsd": item.get("pricePerUnitUsd"),
            "trialMinutes": item.get("trialMinutes"),
        })
    return out


def _price_requires_human(pricing: list[dict[str, Any]]) -> bool:
    per_call_models = {
        "PRICE_PER_DATASET_ITEM",
        "PRICE_PER_EVENT",
        "PRICE_PER_RESULT",
        "PAY_PER_DATASET_ITEM",
        "PAY_PER_EVENT",
        "PAY_PER_RESULT",
    }
    for item in pricing:
        model = str(item.get("pricingModel") or "").upper()
        price = item.get("pricePerUnitUsd")
        if not isinstance(price, (int, float)):
            continue
        if model in per_call_models and price >= MAX_PRICE_PER_COMPANY_CALL_USD:
            return True
    return False


def preflight_actors(actors: list[ActorSpec]) -> list[dict[str, Any]]:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        return [
            {
                "label": a.label,
                "actor_id": a.actor_id,
                "alive": False,
                "aborted": True,
                "reason": "APIFY_TOKEN missing; cannot verify actor metadata",
                "schema_ok": False,
                "fields": [],
            }
            for a in actors
        ]

    ApifyClient = _import_apify_client()
    client = ApifyClient(token)
    results: list[dict[str, Any]] = []
    for actor in actors:
        try:
            actor_client = client.actor(actor.actor_id)
            meta = _actor_get_with_timeout(actor_client)
            if not meta:
                result = {
                    "label": actor.label,
                    "actor_id": actor.actor_id,
                    "alive": False,
                    "aborted": True,
                    "reason": "actor ID did not resolve on Apify",
                    "schema_ok": False,
                    "fields": [],
                    "pricing": [],
                    "exampleRunInput": None,
                    "stats": {},
                }
                results.append(result)
                continue
            schema = _extract_schema(meta)
            schema_ok, reason, fields = _input_acceptance(schema)
            if not schema_ok:
                example_ok, example_reason, example_fields = _example_acceptance(meta.get("exampleRunInput"))
                if example_ok:
                    schema_ok = True
                    reason = example_reason
                    fields = example_fields
            pricing = _pricing_summary(meta)
            price_hold = _price_requires_human(pricing)
            if price_hold:
                schema_ok = False
                reason = f"pricing requires human recalibration: {pricing}"
            result = {
                "label": actor.label,
                "actor_id": actor.actor_id,
                "alive": bool(meta),
                "aborted": not schema_ok,
                "reason": reason,
                "schema_ok": schema_ok,
                "fields": fields,
                "pricing": pricing,
                "exampleRunInput": meta.get("exampleRunInput") if isinstance(meta, dict) else None,
                "stats": meta.get("stats") if isinstance(meta, dict) else None,
                "raw_meta_keys": sorted(meta.keys()) if isinstance(meta, dict) else [],
            }
        except Exception as exc:
            result = {
                "label": actor.label,
                "actor_id": actor.actor_id,
                "alive": False,
                "aborted": True,
                "reason": f"actor ID did not resolve or metadata lookup failed: {exc}",
                "schema_ok": False,
                "fields": [],
                "pricing": [],
                "exampleRunInput": None,
                "stats": None,
            }
        results.append(result)
    return results


def _run_input_variants(row: dict[str, Any]) -> list[dict[str, Any]]:
    name = row.get("name") or ""
    website = row.get("website") or ""
    linkedin = row.get("linkedin_url") or ""
    body: dict[str, Any] = {
        "searchQuery": name,
        "maxResults": 5,
        "companyUrls": [],
    }
    if website:
        body["website"] = website
    if linkedin:
        body["linkedinUrl"] = linkedin
    return [body]


def call_actor_for_row(client: Any, actor_id: str, row: dict[str, Any]) -> dict[str, Any]:
    """Try conservative input variants until one actor run succeeds.

    This starts paid actor runs; caller must gate with --live and budget checks.
    """
    errors = []
    for run_input in _run_input_variants(row):
        try:
            run = client.actor(actor_id).call(run_input=run_input, timeout_secs=600)
            ds_id = run.get("defaultDatasetId") if run else None
            items = list(client.dataset(ds_id).iterate_items()) if ds_id else []
            return {
                "run_input": run_input,
                "run": run,
                "items": items,
                "spent_usd": float((run or {}).get("usageTotalUsd") or 0.0),
            }
        except Exception as exc:
            errors.append({"run_input": run_input, "error": str(exc)})
    return {"run_input": None, "run": None, "items": [], "spent_usd": 0.0, "errors": errors}


def _is_non_null_record(items: list[Any]) -> bool:
    return any(bool(item) for item in items)


def _list_from_any(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _first_record(items: list[Any]) -> dict[str, Any]:
    for item in items:
        if isinstance(item, dict) and item:
            return item
    return {}


def _count_rounds(record: dict[str, Any]) -> int:
    keys = ("fundingRounds", "funding_rounds", "rounds", "investments", "funding")
    return max((len(_list_from_any(record.get(k))) for k in keys), default=0)


def _count_investors(record: dict[str, Any]) -> int:
    keys = ("investors", "leadInvestors", "lead_investors", "fundingInvestors")
    return max((len(_list_from_any(record.get(k))) for k in keys), default=0)


def _count_founders_with_history(record: dict[str, Any]) -> int:
    founders = _list_from_any(record.get("founders") or record.get("founder"))
    count = 0
    for founder in founders:
        if not isinstance(founder, dict):
            continue
        history = founder.get("priorCompanies") or founder.get("previousCompanies") or founder.get("jobs")
        if _list_from_any(history):
            count += 1
    return count


def _count_categories(record: dict[str, Any]) -> int:
    return max(
        len(_list_from_any(record.get(k)))
        for k in ("categories", "categoryGroups", "industries", "tags")
    )


def _has_last_funding_date(record: dict[str, Any]) -> bool:
    keys = ("lastFundingDate", "last_funding_date", "lastFundingAt", "last_funding_at")
    return any(bool(record.get(k)) for k in keys)


def _schema_signature(record: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(record.keys()))


def score_actor(sample: list[dict[str, Any]], payloads: list[dict[str, Any]]) -> dict[str, Any]:
    by_company = {int(p["company_id"]): p for p in payloads}
    records = [_first_record(by_company.get(int(r["id"]), {}).get("items", [])) for r in sample]
    non_null = [r for r in records if r]
    uk_ids = {int(r["id"]) for r in sample if r.get("stratum") == "brave1_prozorro"}
    uk_non_null = [
        _first_record(by_company.get(cid, {}).get("items", []))
        for cid in uk_ids
        if _first_record(by_company.get(cid, {}).get("items", []))
    ]
    total_spend = sum(float(p.get("spent_usd") or 0.0) for p in payloads)
    signatures = Counter(_schema_signature(r) for r in non_null)
    denom = len(sample) or 1
    non_null_denom = len(non_null) or 1
    return {
        "coverage_rate": len(non_null) / denom,
        "funding_rounds_detail": sum(_count_rounds(r) for r in non_null) / non_null_denom,
        "investors_completeness": sum(_count_investors(r) for r in non_null) / non_null_denom,
        "founders_depth": sum(_count_founders_with_history(r) for r in non_null) / non_null_denom,
        "categories_count": sum(_count_categories(r) for r in non_null) / non_null_denom,
        "last_funding_date_present": sum(1 for r in non_null if _has_last_funding_date(r)) / non_null_denom,
        "schema_consistency": (max(signatures.values()) / len(non_null)) if non_null else 0.0,
        "cost_per_company": total_spend / denom,
        "ukrainian_coverage_rate": len(uk_non_null) / (len(uk_ids) or 1),
        "total_spend": total_spend,
        "records_returned": len(non_null),
    }


def run_live(sample: list[dict[str, Any]], preflight: list[dict[str, Any]], budget_cap: float) -> dict[str, Any]:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise SystemExit("ABORT: APIFY_TOKEN missing")
    survivors = [p for p in preflight if not p["aborted"]]
    if not survivors:
        raise SystemExit("ABORT: no surviving actors after preflight")
    ApifyClient = _import_apify_client()
    client = ApifyClient(token)
    cumulative = 0.0
    results: dict[str, Any] = {}
    for actor in survivors:
        if cumulative >= budget_cap:
            results[actor["label"]] = {"aborted": True, "reason": "budget cap reached before actor"}
            continue
        actor_dir = ARTIFACT_ROOT / _safe_actor_label(actor["actor_id"])
        payloads = []
        actor_spend = 0.0
        actor_completed = 0
        abort_actor = False
        for row in sample:
            if abort_actor:
                payloads.append({
                    "company_id": row["id"],
                    "company_name": row["name"],
                    "aborted": True,
                    "reason": "actor exceeded $0.15/company live cost cutoff",
                })
                continue
            if cumulative >= budget_cap:
                payloads.append({"company_id": row["id"], "aborted": True, "reason": "budget cap reached"})
                continue
            t0 = time.time()
            payload = call_actor_for_row(client, actor["actor_id"], row)
            payload["company_id"] = row["id"]
            payload["company_name"] = row["name"]
            payload["elapsed_s"] = round(time.time() - t0, 1)
            cumulative += float(payload.get("spent_usd") or 0.0)
            actor_spend += float(payload.get("spent_usd") or 0.0)
            actor_completed += 1
            payload["cumulative_spend_usd"] = round(cumulative, 4)
            payloads.append(payload)
            _write_json(actor_dir / f"{row['id']}.json", payload)
            if actor_completed and (actor_spend / actor_completed) > MAX_PRICE_PER_COMPANY_CALL_USD:
                abort_actor = True
        results[actor["label"]] = {
            "actor_id": actor["actor_id"],
            "payloads": payloads,
            "score": score_actor(sample, payloads),
        }
    results["_cumulative_spend_usd"] = round(cumulative, 4)
    return results


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(out)


def write_report(
    *,
    sample: list[dict[str, Any]],
    preflight: list[dict[str, Any]],
    live_results: dict[str, Any] | None,
    mode: str,
) -> None:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    sample_rows = [
        [r["id"], r["name"], r.get("source"), r.get("tier"), r.get("stratum"), r.get("website") or "", r.get("linkedin_url") or ""]
        for r in sample
    ]
    preflight_rows = [
        [
            p["label"],
            p["actor_id"],
            p["alive"],
            p["schema_ok"],
            p["aborted"],
            p["reason"],
            json.dumps(p.get("pricing") or [], default=str),
        ]
        for p in preflight
    ]
    lines = [
        "# Crunchbase Actor Bake-Off",
        "",
        f"Generated: {now}",
        f"Mode: `{mode}`",
        "",
        "This is a bake-off report only. No production collector, schema migration, or production-table writes are part of this harness.",
        "",
        "## Sample Composition",
        "",
        _markdown_table(["id", "name", "source", "tier", "stratum", "website", "linkedin_url"], sample_rows),
        "",
        "## Pre-Flight Results",
        "",
        _markdown_table(["label", "actor_id", "alive", "schema_ok", "aborted", "reason", "pricing"], preflight_rows),
        "",
    ]
    if live_results:
        score_rows = []
        for label, result in live_results.items():
            if label.startswith("_") or result.get("aborted"):
                continue
            score = result.get("score", {})
            score_rows.append([
                label,
                f"{score.get('coverage_rate', 0):.1%}",
                f"{score.get('funding_rounds_detail', 0):.2f}",
                f"{score.get('investors_completeness', 0):.2f}",
                f"{score.get('founders_depth', 0):.2f}",
                f"{score.get('categories_count', 0):.2f}",
                f"{score.get('last_funding_date_present', 0):.1%}",
                f"{score.get('schema_consistency', 0):.1%}",
                f"${score.get('cost_per_company', 0):.4f}",
                f"{score.get('ukrainian_coverage_rate', 0):.1%}",
            ])
        lines.extend([
            "## Per-Actor Scorecard",
            "",
            _markdown_table([
                "actor", "coverage", "rounds", "investors", "founders_history",
                "categories", "last_funding_date", "schema_consistency",
                "cost/company", "ukrainian_coverage",
            ], score_rows),
            "",
            f"Cumulative spend recorded by actor runs: `${live_results.get('_cumulative_spend_usd', 0):.4f}`",
            "",
            "## Winner Recommendation",
            "",
            "_Pending human review of the scorecard and raw JSON artifacts._",
            "",
            "## Cost Projection",
            "",
            f"Production baseline cohort: `{BASELINE_COHORT_SIZE}` enriched companies. Multiply each actor's actual `cost/company` by `{BASELINE_COHORT_SIZE}`.",
            "",
        ])
    else:
        lines.extend([
            "## Per-Actor Scorecard",
            "",
            "_Not run yet. Use `--live` only after preflight is acceptable and the operator approves paid actor calls._",
            "",
            "## Winner Recommendation",
            "",
            "_Pending live bake-off._",
            "",
        ])
    lines.extend([
        "## Caveats",
        "",
        "- Ukrainian coverage is expected to be lower than US/EU venture-backed coverage.",
        "- Actor input schemas and pricing must be verified before live spend.",
        "- Raw artifacts are under `docs/artifacts/crunchbase_bakeoff/`.",
        "",
    ])
    REPORT_PATH.write_text("\n".join(lines))


def write_followups() -> None:
    text = """# Crunchbase Bake-Off Followups

These are staging notes only. Do not merge into `docs/DECISIONS.md` or
`docs/QUESTIONS_TO_ANSWER.md` until concurrent Phase 2d.1 documentation work
has wrapped.

## Future Decision

- **D-019 draft:** Production Crunchbase collector choice after human review of
  `docs/CRUNCHBASE_BAKEOFF.md`.

## Open Questions

- Should the production Crunchbase collector run only on enriched/scored
  companies, or on any company with website/linkedin_url?
- Should Crunchbase data remain as raw JSON plus memo-time extraction, or be
  normalized into funding-round tables?
- What minimum coverage rate justifies productionizing the selected actor?
"""
    FOLLOWUPS_PATH.write_text(text)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crunchbase actor bake-off harness.")
    p.add_argument("--dry-run", action="store_true", help="Preflight and write sample/report only. No paid actor calls.")
    p.add_argument("--live", action="store_true", help="Run surviving actors. This can spend Apify credits.")
    p.add_argument("--budget-cap", type=float, default=DEFAULT_BUDGET_CAP_USD)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--skip-preflight", action="store_true", help="Use only for offline sample/report generation.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.live and args.dry_run:
        raise SystemExit("Choose either --live or --dry-run, not both.")
    if not args.live:
        args.dry_run = True

    sample = load_sample(seed=args.seed)
    write_sample(sample)
    if args.skip_preflight:
        preflight = [
            {
                "label": a.label,
                "actor_id": a.actor_id,
                "alive": None,
                "schema_ok": None,
                "aborted": True,
                "reason": "preflight skipped",
                "fields": [],
            }
            for a in ACTORS
        ]
    else:
        preflight = preflight_actors(ACTORS)
    _write_json(ARTIFACT_ROOT / "preflight.json", preflight)

    live_results = None
    if args.live:
        aborts = [p for p in preflight if p["aborted"]]
        survivors = [p for p in preflight if not p["aborted"]]
        print(f"preflight: {len(survivors)} survivor(s), {len(aborts)} aborted")
        if len(survivors) < 2:
            raise SystemExit("ABORT: fewer than 2 surviving actors after preflight")
        live_results = run_live(sample, preflight, budget_cap=args.budget_cap)
        _write_json(ARTIFACT_ROOT / "live_summary.json", live_results)
    else:
        print("dry-run complete: no paid actors called")

    write_report(
        sample=sample,
        preflight=preflight,
        live_results=live_results,
        mode="live" if args.live else "dry-run",
    )
    write_followups()
    print(f"sample:    {ARTIFACT_ROOT / 'sample.json'}")
    print(f"preflight: {ARTIFACT_ROOT / 'preflight.json'}")
    print(f"report:    {REPORT_PATH}")
    print(f"followups: {FOLLOWUPS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
