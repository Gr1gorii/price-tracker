#!/bin/zsh
# Local collector run (launchd fires it hourly; the gate makes it collect once per slot).
# Optional: BFP_GIT_SYNC=1 to commit & push collected data.
set -u
cd "${0:A:h}/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
LOG_DIR="$HOME/Library/Logs/bfp"; mkdir -p "$LOG_DIR"
{
  echo "=== $(date '+%F %T %Z') ==="
  out=$(uv run bfp collect --runner local --scheduled 2>&1); rc=$?
  echo "$out"
  if [[ $rc -eq 0 && "$out" != nothing\ to\ do* ]]; then
    uv run bfp health --alert
    if [[ "${BFP_GIT_SYNC:-0}" == "1" ]]; then
      git add data/observations data/health data/state
      git diff --cached --quiet || git commit -m "data: local run $(date '+%F %H:%M')" && git pull --rebase --autostash && git push
    fi
  fi
} >> "$LOG_DIR/collector.log" 2>&1
