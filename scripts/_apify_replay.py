"""Replay an existing Apify dataset through _process_org without paying.

Usage:
    python scripts/_apify_replay.py <RUN_ID>
    python scripts/_apify_replay.py <RUN_ID> --bypass-filter

Apify keeps run datasets indefinitely; iterating an existing dataset is free.
This re-uses the leads paid for in a previous actor run, exercising the
founder/contact-write path against the current DB state (which may now
have companies rows that didn't exist when the original run executed).

The baseline filter is applied by default (matches live-run behaviour).
Pass --bypass-filter for a faithful re-run under the pre-filter contract.
"""
import os
import sys
import sqlite3
import logging
from pathlib import Path

# Make src/ importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv
from apify_client import ApifyClient

# Load env from project root explicitly to avoid the stdin-frame issue
load_dotenv(dotenv_path=ROOT / ".env")

from collectors.apify_filter import BaselineFilter
from collectors.base import DB_PATH
from collectors.apify_leads import (
    _build_canonical_name_lookup, _group_by_org, _process_org,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")

_pos_args = [a for a in sys.argv[1:] if not a.startswith("--")]
if len(_pos_args) != 1:
    print("usage: python scripts/_apify_replay.py <RUN_ID> [--bypass-filter]",
          file=sys.stderr)
    sys.exit(2)
run_id = _pos_args[0]

token = os.environ.get("APIFY_TOKEN")
if not token:
    raise SystemExit("APIFY_TOKEN missing")
client = ApifyClient(token)

run = client.run(run_id).get()
if not run:
    raise SystemExit(f"run {run_id} not found")
ds_id = run["defaultDatasetId"]
print(f"replaying run {run_id} — dataset {ds_id}")
items = list(client.dataset(ds_id).iterate_items())
print(f"got {len(items)} items from existing dataset (no actor charge)\n")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
canonical_lookup = _build_canonical_name_lookup(conn)
print(f"canonical_name lookup: {len(canonical_lookup)} entries\n")

grouped = _group_by_org(items)
print(f"grouped into {len(grouped)} unique orgs\n")

# Replay applies the same baseline filter as live runs — orgs that fail
# the filter get logged to rejected_orgs (idempotent), founders/contacts
# are not written. To replay an old dataset under the *original*
# unfiltered contract, pass --bypass-filter.
bypass_filter = "--bypass-filter" in sys.argv[2:]
baseline_filter = None if bypass_filter else BaselineFilter.from_yaml()
if baseline_filter is None:
    print("baseline filter BYPASSED (operator backfill mode)")
else:
    print(f"baseline filter loaded: {len(baseline_filter.rules)} rule(s)")

stats = {"new": 0, "skipped": 0, "rejected": 0, "fnd": 0, "ct": 0}
for bucket in grouped.values():
    s = _process_org(
        bucket, conn, canonical_lookup,
        no_write=False,
        baseline_filter=baseline_filter,
        bypass_filter=bypass_filter,
    )
    stats["new"]      += int(s.new_lead)
    stats["skipped"]  += int(s.skipped)
    stats["rejected"] += int(s.rejected)
    stats["fnd"]      += s.founders_added
    stats["ct"]       += s.contacts_added
conn.close()

print()
print("=== replay summary ===")
print(f"  unique_orgs    : {len(grouped)}")
print(f"  new_raw_leads  : {stats['new']}      (expect 0 — original run already wrote them)")
print(f"  skipped_excl   : {stats['skipped']}")
print(f"  rejected_base  : {stats['rejected']}")
print(f"  founders_added : {stats['fnd']}")
print(f"  contacts_added : {stats['ct']}")
