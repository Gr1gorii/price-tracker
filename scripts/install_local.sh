#!/bin/zsh
# Installs a launchd agent that runs scripts/run_local.sh every hour at :05.
# Collection happens only inside the Rome slot windows (08:00 / 20:00), once per slot.
# Uninstall: launchctl bootout gui/$(id -u)/it.bfp.collector && rm ~/Library/LaunchAgents/it.bfp.collector.plist
set -eu
ROOT="${0:A:h:h}"
PLIST="$HOME/Library/LaunchAgents/it.bfp.collector.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/bfp"
sed "s|__RUN_LOCAL__|$ROOT/scripts/run_local.sh|; s|__LOG__|$HOME/Library/Logs/bfp/launchd.log|g" \
  "$ROOT/scripts/launchd/it.bfp.collector.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/it.bfp.collector" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $PLIST  (logs: ~/Library/Logs/bfp/collector.log)"
