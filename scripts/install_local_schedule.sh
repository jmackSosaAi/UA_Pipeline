#!/bin/bash
# Install the defense-press launchd agent for the current user.
# - Substitutes __PROJECT_ROOT__ with the absolute repo path
# - Copies the rendered plist into ~/Library/LaunchAgents/
# - Loads (or reloads) the job via launchctl
#
# To uninstall:
#   launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.uapipeline.defense_press.plist
#   rm ~/Library/LaunchAgents/com.uapipeline.defense_press.plist
#
# Usage:
#   bash scripts/install_local_schedule.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.uapipeline.defense_press"
SRC_PLIST="$PROJECT_ROOT/scripts/launchd/$LABEL.plist"
DEST_DIR="$HOME/Library/LaunchAgents"
DEST_PLIST="$DEST_DIR/$LABEL.plist"

if [ ! -f "$SRC_PLIST" ]; then
    echo "Source plist missing: $SRC_PLIST" >&2
    exit 1
fi

mkdir -p "$DEST_DIR"

# Render the template into the destination, substituting the project path.
sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" "$SRC_PLIST" > "$DEST_PLIST"

# If the agent is already loaded, unload it first so launchctl picks up the
# rendered plist. `bootout` is the modern verb; old `unload` still works.
if launchctl list | grep -q "$LABEL"; then
    echo "Reloading existing agent..."
    launchctl bootout "gui/$(id -u)" "$DEST_PLIST" 2>/dev/null || true
fi

launchctl bootstrap "gui/$(id -u)" "$DEST_PLIST"

echo
echo "Installed launchd agent: $LABEL"
echo "  plist:  $DEST_PLIST"
echo "  source: $SRC_PLIST"
echo "  schedule: daily at 07:00 local time"
echo
echo "Useful commands:"
echo "  launchctl list | grep $LABEL                 # show status"
echo "  launchctl kickstart -p gui/\$(id -u)/$LABEL   # run now"
echo "  tail -f $PROJECT_ROOT/logs/launchd_*.log     # watch output"
echo "  bash scripts/install_local_schedule.sh       # re-render & reload"
echo
echo "To uninstall:"
echo "  launchctl bootout gui/\$(id -u) $DEST_PLIST"
echo "  rm $DEST_PLIST"
