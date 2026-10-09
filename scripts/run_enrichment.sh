#!/bin/bash
cd "$(dirname "$0")/.."
source venv/bin/activate
caffeinate -di python -W ignore src/enrich.py "$@"
