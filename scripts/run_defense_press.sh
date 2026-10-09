#!/bin/bash
# Defense press daily wrapper — runs the RSS collector then promotes leads.
#
# Designed to be portable across local machines (launchd), CI runners
# (GitHub Actions), and containers (Render/Fly.io). No hardcoded paths.
#
# Usage:
#   bash scripts/run_defense_press.sh
#
# Environment variables:
#   LOG_DIR         — log output directory (default: <project>/logs)
#   ANTHROPIC_API_KEY — required for Claude extraction (read from .env if absent)
#
# Exit code: 0 on success, non-zero matching the failing step.

set -euo pipefail

# cd into project root regardless of how the script was invoked.
cd "$(dirname "$0")/.."

# Activate venv if present (local dev). Containers / CI runners use the
# Python that's already on PATH and skip this step.
if [ -f "venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

LOG_DIR="${LOG_DIR:-./logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/defense_press_$(date +%Y-%m-%d).log"

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# Run a labeled pipeline step. Tees output to both terminal and log file
# (pipefail propagates the inner command's exit code through tee).
run_step() {
    local label="$1"
    shift
    echo "[$(ts)] STEP: $label" | tee -a "$LOG_FILE"
    local code=0
    "$@" 2>&1 | tee -a "$LOG_FILE" || code=$?
    echo "[$(ts)] STEP: $label exit=$code" | tee -a "$LOG_FILE"
    return $code
}

run_step "defense_press_collector" python -W ignore -m src.collectors.defense_press
run_step "promote_leads"           python -W ignore -m src.collectors.promote

echo "[$(ts)] All steps complete (log: $LOG_FILE)" | tee -a "$LOG_FILE"
