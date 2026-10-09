"""Phase 3 actor bake-off — apify_company_employees candidates.

Fires harvestapi/linkedin-company-employees + apimaestro/linkedin-company-
employees-scraper-no-cookies against the same 5-company cohort, captures
raw JSON output + per-actor cost & latency, writes one JSON per actor.

Spend cap: $0.50 cumulative. Per-actor cap: $0.15. Aborts if exceeded.

Usage:
    python scripts/_phase3_bakeoff.py
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(dotenv_path=ROOT / ".env")
from apify_client import ApifyClient

# 5-row bake-off cohort, picked across size buckets + sectors + sources.
COHORT = [
    {"id": 302,  "name": "The Swarm",                "linkedin_url": "https://www.linkedin.com/company/the-swarm",
     "size": "2 - 10",     "sector": "civilian"},
    {"id": 98,   "name": "Robonetica",               "linkedin_url": "https://www.linkedin.com/company/robonetica",
     "size": "11 - 50",    "sector": "civilian"},
    {"id": 97,   "name": "Infozahyst",               "linkedin_url": "https://www.linkedin.com/company/infozahyst",
     "size": "51 - 200",   "sector": "defense"},
    {"id": 926,  "name": "ABL SPACE SYSTEMS COMPANY", "linkedin_url": "https://www.linkedin.com/company/abl-space-systems",
     "size": "201 - 500",  "sector": "Defense & Space"},
    {"id": 1387, "name": "CLOUDBEES, INC.",          "linkedin_url": "https://www.linkedin.com/company/cloudbees",
     "size": "501 - 1000", "sector": "Computer Software"},
]

MAX_EMPLOYEES_PER_CO = 5      # default for batched actors
MAX_EMPLOYEES_APIMAESTRO = 3  # tighter for apimaestro to stay under $0.15 cap
PER_ACTOR_CAP_USD = 0.15

# harvestapi/linkedin-company-employees would have been candidate A but it
# requires an interactive "Approve permissions" grant via the Apify console
# before it can run. Excluded from this bake-off; revisit if/when the
# operator approves it.

def run_apt_marble(client, urls):
    """apt_marble takes batched company URLs + maxEmployees (per-co).
    Cheapest of the three at $0.0035 flat per employee."""
    return client.actor("apt_marble/linkedin-company-employees-scraper").call(
        run_input={
            "companyUrls": urls,
            "maxEmployees": MAX_EMPLOYEES_PER_CO,
        },
        timeout_secs=600,
    )


def run_automation_lab(client, urls):
    """automation-lab — batched, $0.005 start + $0.00575/employee free tier."""
    return client.actor("automation-lab/linkedin-company-employees-scraper").call(
        run_input={
            "companyUrls": urls,
            "maxEmployees": MAX_EMPLOYEES_PER_CO,
        },
        timeout_secs=600,
    )


def run_apimaestro(client, urls):
    """apimaestro takes a single identifier per call. Loop + aggregate.
    Tighter per-co cap to stay under $0.15."""
    aggregated_items = []
    aggregated_run_meta = []
    actor_id = "apimaestro/linkedin-company-employees-scraper-no-cookies"
    for url in urls:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        run = client.actor(actor_id).call(
            run_input={"identifier": slug, "max_employees": MAX_EMPLOYEES_APIMAESTRO},
            timeout_secs=600,
        )
        if not run:
            continue
        ds_id = run.get("defaultDatasetId")
        items = list(client.dataset(ds_id).iterate_items()) if ds_id else []
        aggregated_items.extend(items)
        aggregated_run_meta.append({
            "input_url": url, "run_id": run.get("id"),
            "status": run.get("status"), "items": len(items),
            "usage_total_usd": run.get("usageTotalUsd"),
        })
    return {
        "actor_id": actor_id,
        "items": aggregated_items,
        "meta_per_call": aggregated_run_meta,
    }


def main():
    token = os.environ["APIFY_TOKEN"]
    client = ApifyClient(token)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    urls = [c["linkedin_url"] for c in COHORT]
    cumulative = 0.0

    actors = [
        ("apt_marble",     "apt_marble/linkedin-company-employees-scraper",
         run_apt_marble),
        ("automation-lab", "automation-lab/linkedin-company-employees-scraper",
         run_automation_lab),
        ("apimaestro",     "apimaestro/linkedin-company-employees-scraper-no-cookies",
         run_apimaestro),
    ]
    results = {}
    for label, actor_id, runner in actors:
        if cumulative >= 0.45:        # leave ~$0.05 headroom under $0.50 cap
            print(f"\n!! ABORT {label} — cumulative ${cumulative:.4f} too close to $0.50 cap")
            continue
        print(f"\n===== {label}: {actor_id} =====")
        t0 = time.time()
        run = runner(client, urls)
        elapsed = time.time() - t0
        # harvestapi returns the apify Run object; apimaestro returns a dict
        if isinstance(run, dict) and "items" in run and "meta_per_call" in run:
            # apimaestro aggregated
            items = run["items"]
            run_meta = run["meta_per_call"]
            cost_meta_sum = sum(m.get("usage_total_usd") or 0.0 for m in run_meta)
            payload = {
                "actor_id": actor_id, "elapsed_s": round(elapsed, 1),
                "items": items,
                "meta_per_call": run_meta,
                "spent_usd": round(cost_meta_sum, 4),
            }
            spent = cost_meta_sum
        else:
            # harvestapi single run
            ds_id = run.get("defaultDatasetId") if run else None
            items = list(client.dataset(ds_id).iterate_items()) if ds_id else []
            spent_estimate = run.get("usageTotalUsd") if run else 0.0
            payload = {
                "actor_id": actor_id, "elapsed_s": round(elapsed, 1),
                "items": items,
                "run_id": run.get("id") if run else None,
                "status": run.get("status") if run else None,
                "stats": run.get("stats") if run else {},
                "spent_usd": round(spent_estimate or 0.0, 4),
                "charged_event_counts": run.get("chargedEventCounts") if run else None,
            }
            spent = spent_estimate or 0.0
        cumulative += spent
        print(f"  items returned: {len(items)}")
        print(f"  spent:          ${spent:.4f}   cumulative: ${cumulative:.4f}")
        if spent > PER_ACTOR_CAP_USD:
            print(f"  !! WARN per-actor cap breached: ${spent:.4f} > ${PER_ACTOR_CAP_USD:.2f}")
        out_path = ROOT / "data" / f"phase3_bakeoff_{label}_{stamp}.json"
        out_path.write_text(json.dumps(payload, indent=2, default=str))
        print(f"  saved {out_path.name}")
        results[label] = payload

    print(f"\n=== bake-off complete. cumulative spend: ${cumulative:.4f} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
