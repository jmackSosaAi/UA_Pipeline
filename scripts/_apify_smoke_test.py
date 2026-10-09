"""Verify Apify API token works. Costs nothing, no actor runs."""
import os
from dotenv import load_dotenv
from apify_client import ApifyClient

load_dotenv()
token = os.environ.get("APIFY_TOKEN")
if not token:
    raise SystemExit("APIFY_TOKEN missing from .env — add it and try again.")
if not token.startswith("apify_api_"):
    print(f"WARNING: token doesn't start with 'apify_api_' — double-check you copied the right thing")

client = ApifyClient(token)

try:
    me = client.user("me").get()
    print(f"Connected as: {me.get('username')}")
    print(f"Email:        {me.get('email')}")
    print(f"Plan:         {me.get('plan', 'free')}")
except Exception as e:
    raise SystemExit(f"Auth failed: {type(e).__name__}: {e}")
