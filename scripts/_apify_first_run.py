"""Test braveleads/leads-finder with smart defense-relevant filter.

Strategy: filter for executive titles + a defense industry keyword.
Each result = a founder/CEO + their company. Discovery and founder
enrichment in one call. Costs ~$0.02 on success (10 leads × $1.70/1k).
"""
import json
import os
from dotenv import load_dotenv
from apify_client import ApifyClient

load_dotenv()
client = ApifyClient(os.environ["APIFY_TOKEN"])

ACTOR_ID = "braveleads/leads-finder-linkedin-apollo-leads-generator"

run_input = {
    "maxResults": 100,
    "contactEmailStatus": "verified",
    "personTitles": ["CEO", "Founder", "Co-Founder", "Chief Executive Officer"],
    "industryKeywords": ["counter-drone"],
    "companyLocation": {"country": "United States"},
}

print(f"Calling actor: {ACTOR_ID}")
print(f"Input: {json.dumps(run_input, indent=2)}\n")

try:
    run = client.actor(ACTOR_ID).call(run_input=run_input)
except Exception as e:
    print(f"Actor call failed: {type(e).__name__}: {e}")
    print(f"\nIf the error is a schema validation error, on the actor page:")
    print(f"1. Click 'Restore example input' (bottom of the Input form)")
    print(f"2. Click the 'JSON' toggle (top of the form)")
    print(f"3. Copy the example JSON and paste it here so we can fix the run_input dict")
    raise SystemExit(1)

print(f"Run finished. Status: {run.get('status')}")
print(f"Run ID:           {run.get('id')}")
stats = run.get("stats", {}) or {}
print(f"Compute units:    {stats.get('computeUnits', 'unknown')}")
print(f"Runtime (sec):    {stats.get('runTimeSecs', 'unknown')}\n")

print("=== Results ===")
items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
print(f"Got {len(items)} items.\n")

for i, item in enumerate(items):
    print(f"--- item {i+1} ---")
    print(f"All field names: {sorted(item.keys())}")
    print(f"\nFull item content:")
    truncated = {}
    for k, v in item.items():
        if isinstance(v, str) and len(v) > 200:
            truncated[k] = v[:200] + f"...[truncated, total {len(v)} chars]"
        else:
            truncated[k] = v
    print(json.dumps(truncated, indent=2))
    print()
