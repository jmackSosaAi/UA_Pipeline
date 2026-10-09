#!/bin/bash
# Wrapper for the priority SBIR enrichment cohort. Mirrors run_enrichment.sh.
# Pass --limit N etc. through to the Python entrypoint.
cd "$(dirname "$0")/.."
source venv/bin/activate
caffeinate -di python -W ignore -m src.enrich_priority_sbir "$@"
