#!/bin/zsh
# Local collector run (launchd fires it hourly; the gate makes it collect once per slot).
# Optional: BFP_GIT_SYNC=1 to commit & push collected data.
set -u
cd "${0:A:h}/.."
[[ -f .env ]] && { set -a; source ./.env; set +a; }   # BFP_CONTACT_EMAIL, BFP_GIT_SYNC, TELEGRAM_*
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
LOG_DIR="$HOME/Library/Logs/bfp"; mkdir -p "$LOG_DIR"
{
  echo "=== $(date '+%F %T %Z') ==="
  # caffeinate -i: no idle sleep while collecting (a closed lid on battery still sleeps)
  out=$(caffeinate -i uv run bfp collect --runner local --scheduled 2>&1); rc=$?
  echo "$out"
  if [[ $rc -eq 0 && "$out" != nothing\ to\ do* ]]; then
    uv run bfp health --alert
    if [[ "${BFP_GIT_SYNC:-0}" == "1" ]]; then
      git add data/observations data/health data/state
      git diff --cached --quiet || git commit -q -m "data: local run $(date '+%F %H:%M')"
      git pull -q --rebase --autostash && git push -q
    fi
  fi
} >> "$LOG_DIR/collector.log" 2>&1
