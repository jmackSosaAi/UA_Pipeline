"""Analyze the most recent run of braveleads/leads-finder against existing DB.

Free — pulls from Apify's stored dataset, doesn't run the actor again.
"""
import os
import sqlite3
from collections import Counter
from dotenv import load_dotenv
from apify_client import ApifyClient

load_dotenv()
client = ApifyClient(os.environ["APIFY_TOKEN"])
ACTOR_ID = "braveleads/leads-finder-linkedin-apollo-leads-generator"

# Find most recent run for this actor
runs = client.actor(ACTOR_ID).runs().list(limit=1, desc=True).items
if not runs:
    raise SystemExit("No runs found")
run = runs[0]
print(f"Analyzing run {run['id']} (status: {run['status']}, started: {run['startedAt']})\n")

items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
print(f"Total items: {len(items)}\n")

# Unique companies
companies = [it.get("organizationName", "?") for it in items]
unique = set(companies)
print(f"Unique organizations: {len(unique)} (across {len(items)} leads)\n")

# Seniority distribution
sen = Counter(it.get("seniority", "?") for it in items)
print("Seniority distribution:")
for k, v in sen.most_common():
    print(f"  {k}: {v}")
print()

# Industry distribution (top 10)
ind = Counter(it.get("organizationIndustry", "?") for it in items)
print("Top organization industries:")
for k, v in ind.most_common(10):
    print(f"  {v:3d}  {k}")
print()

# Overlap with existing DB
conn = sqlite3.connect("data/companies.db")
existing = {r[0].lower().strip() for r in conn.execute("SELECT name FROM companies").fetchall() if r[0]}
existing_websites = {r[0].lower().strip().rstrip("/") for r in conn.execute("SELECT website FROM companies WHERE website IS NOT NULL").fetchall() if r[0]}

new_co = []
already = []
for it in items:
    name = (it.get("organizationName") or "").lower().strip()
    site = (it.get("organizationWebsite") or "").lower().strip().rstrip("/")
    if name in existing or (site and site in existing_websites):
        already.append(it["organizationName"])
    else:
        new_co.append(it["organizationName"])

print(f"Already in your DB: {len(set(already))} unique companies")
print(f"NEW to your DB:     {len(set(new_co))} unique companies\n")

print("Sample of NEW companies (first 15 unique):")
for c in sorted(set(new_co))[:15]:
    print(f"  - {c}")
