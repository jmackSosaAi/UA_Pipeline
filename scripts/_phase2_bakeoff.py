"""Phase 2 bake-off: company-firmographics enrichment actors.

Fires both candidate actors against the same 5-company cohort, captures
raw JSON output + Apify run stats (cost, latency), and writes per-actor
result files. Computes per-company key fields and aggregate completeness.

Usage:
    python scripts/_phase2_bakeoff.py --probe        # single-URL probe
    python scripts/_phase2_bakeoff.py --full         # full 5-co bake-off
"""
from __future__ import annotations

import argparse
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


COHORT = [
    {"id": 749,  "name": "Picogrid",                   "linkedin_url": "https://www.linkedin.com/company/picogrid"},
    {"id": 1146, "name": "ATA ENGINEERING, INC.",      "linkedin_url": "https://www.linkedin.com/company/ata-engineering-inc"},
    {"id": 1154, "name": "ATLAS SPACE OPERATIONS INC", "linkedin_url": "https://www.linkedin.com/company/atlas-space-operations-inc"},
    {"id": 298,  "name": "Dropla",                     "linkedin_url": "https://www.linkedin.com/company/dropla-tech"},
    {"id": 306,  "name": "Odd Systems",                "linkedin_url": "https://www.linkedin.com/company/odd-systems"},
]

# Per-actor input shape — discovered from each actor's build inputSchema.
#   apimaestro: {"identifier": [slug | full URL, ...]}    (field is required)
#   harvestapi: {"companies": [URL, ...]}
PER_ACTOR_INPUT = {
    "apimaestro/linkedin-company-detail": lambda urls: {"identifier": urls},
    "harvestapi/linkedin-company":        lambda urls: {"companies": urls},
}

CUMULATIVE_BUDGET_CAP_USD = 0.50


def call_actor(client: ApifyClient, actor_id: str, urls: list[str]) -> dict:
    """Single actor call using the per-actor input shape derived from its
    build's inputSchema. Returns the run + dataset items (or error)."""
    builder = PER_ACTOR_INPUT.get(actor_id)
    if builder is None:
        return {"shape": None, "items": [], "error": f"no input builder for {actor_id}"}
    run_input = builder(urls)
    print(f"  [{actor_id}] input={list(run_input.keys())}  n_urls={len(urls)}")
    t0 = time.time()
    try:
        run = client.actor(actor_id).call(run_input=run_input, timeout_secs=600)
    except Exception as e:
        return {"shape": None, "items": [], "error": f"actor.call raised: {e!r}"}
    elapsed = time.time() - t0
    if not run:
        return {"shape": None, "items": [], "error": "no run object returned"}
    status = run.get("status")
    ds_id  = run.get("defaultDatasetId")
    if not ds_id:
        return {"shape": None, "items": [], "error": f"status={status} no datasetId",
                "run_status": status}
    items = list(client.dataset(ds_id).iterate_items())
    print(f"    status={status} items={len(items)} elapsed={elapsed:.1f}s")
    return {
        "shape":       list(run_input.keys())[0],
        "items":       items,
        "run_stats":   run.get("stats", {}),
        "run_status":  status,
        "run_id":      run.get("id"),
        "elapsed_s":   elapsed,
        "run_input":   run_input,
        "usage":       run.get("usage", {}),
        "chargedUsd":  run.get("chargedAmountUsd") or run.get("usageTotalUsd"),
    }


def write_output(actor_id: str, result: dict, stamp: str) -> Path:
    out_dir = ROOT / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = actor_id.replace("/", "_")
    path = out_dir / f"phase2_bakeoff_{safe}_{stamp}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="Single-URL probe (Picogrid only) — cheapest schema check.")
    ap.add_argument("--full",  action="store_true",
                    help="Full 5-company bake-off, both actors.")
    args = ap.parse_args()
    if not (args.probe or args.full):
        ap.error("pass --probe or --full")

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise SystemExit("APIFY_TOKEN missing")
    client = ApifyClient(token)

    actors = [
        ("apimaestro/linkedin-company-detail", "candidateA"),
        ("harvestapi/linkedin-company",        "candidateB"),
    ]
    urls = [c["linkedin_url"] for c in (COHORT[:1] if args.probe else COHORT)]
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    print(f"\n=== bake-off {stamp} | {len(urls)} url(s) | budget cap ${CUMULATIVE_BUDGET_CAP_USD:.2f} ===")
    cumulative_usd = 0.0
    written: list[Path] = []
    for actor_id, label in actors:
        print(f"\n----- {label}: {actor_id} -----")
        if cumulative_usd >= CUMULATIVE_BUDGET_CAP_USD:
            print(f"  BUDGET STOP — cumulative ${cumulative_usd:.4f} >= cap")
            break
        result = call_actor(client, actor_id, urls)
        # estimate spend if charged not present
        spent = result.get("chargedUsd")
        if spent is None:
            n_items = len(result.get("items") or [])
            if "apimaestro" in actor_id:
                spent = n_items * 0.005
            else:
                spent = n_items * 0.004 + 5e-5
        result["estimated_or_charged_usd"] = spent
        cumulative_usd += float(spent or 0)
        path = write_output(actor_id, {"actor": actor_id, "label": label, **result}, stamp)
        written.append(path)
        print(f"  spent: ${spent:.4f}   cumulative: ${cumulative_usd:.4f}")
        print(f"  output: {path.name}")

    print(f"\n=== DONE | total spend: ${cumulative_usd:.4f} ===")
    print("files written:")
    for p in written:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
