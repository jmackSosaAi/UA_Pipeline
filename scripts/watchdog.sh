#!/bin/bash
# Auto-restart a long-running enrichment-style script if it dies.
# Runs for max 12 hours.
#
# Usage:
#   bash scripts/watchdog.sh                                          # default: scripts/run_enrichment.sh
#   bash scripts/watchdog.sh scripts/run_priority_sbir_enrichment.sh  # any wrapper
#   bash scripts/watchdog.sh scripts/run_enrichment.sh -- --limit 50  # extra args after `--`
#
# Liveness check: the script greps the process table for the basename
# of the wrapper, so the same watchdog works for every run_*_enrichment.sh
# variant without code changes.
#
# Logs to logs/watchdog_YYYY-MM-DD.log.

cd "$(dirname "$0")/.."

# ── Args ──────────────────────────────────────────────────────────────────────

WRAPPER="${1:-scripts/run_enrichment.sh}"
shift || true
# Allow `-- --extra args` to separate watchdog args from wrapper args
if [ "${1:-}" = "--" ]; then
    shift
fi

if [ ! -f "$WRAPPER" ]; then
    echo "Watchdog target not found: $WRAPPER" >&2
    exit 2
fi

WRAPPER_BASENAME="$(basename "$WRAPPER")"

# ── Setup ─────────────────────────────────────────────────────────────────────

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/watchdog_$(date +%Y-%m-%d).log"

MAX_RUNTIME=$((12 * 3600))   # 12 hours in seconds
CHECK_INTERVAL=60            # seconds between liveness checks
START_TIME=$(date +%s)

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] $*" | tee -a "$LOG_FILE"
}

is_running() {
    pgrep -f "$WRAPPER_BASENAME" > /dev/null
}

start_target() {
    log "Starting target: $WRAPPER $*"
    nohup bash "$WRAPPER" "$@" \
        >> "$LOG_DIR/${WRAPPER_BASENAME%.sh}_stdout_$(date +%Y-%m-%d).log" 2>&1 &
    sleep 5
}

log "Watchdog starting — target=$WRAPPER  max=12h  interval=${CHECK_INTERVAL}s"

if ! is_running; then
    start_target "$@"
fi

while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TIME))

    if [ "$ELAPSED" -ge "$MAX_RUNTIME" ]; then
        log "Max runtime reached (${MAX_RUNTIME}s) — exiting."
        exit 0
    fi

    if ! is_running; then
        log "Target not running — restarting."
        start_target "$@"
    fi

    sleep "$CHECK_INTERVAL"
done
