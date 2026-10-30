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
  # Backup trigger for the GitHub runner (its cron is sometimes late or dropped): queue the
  # workflow once per new slot; its own gate makes this a no-op if the slot is already done.
  if uv run bfp gate --runner local 2>/dev/null | grep -q '^run=true'; then
    gh workflow run collect.yml >/dev/null 2>&1 && echo "queued GitHub collect (backup trigger)"
  fi
  # caffeinate -i: no idle sleep while collecting (a closed lid on battery still sleeps)
  out=$(caffeinate -i uv run bfp collect --runner local --scheduled 2>&1); rc=$?
  echo "$out"
  if [[ $rc -eq 0 && "$out" != nothing\ to\ do* ]]; then
    uv run bfp health --alert
    if [[ "${BFP_GIT_SYNC:-0}" == "1" ]]; then
      git add data/observations data/health data/state
      git diff --cached --quiet || git commit -q -m "data: local run $(date '+%F %H:%M')"
      # Fetch into FETCH_HEAD only (--refmap=): never updates refs/remotes/origin/main, so a
      # background git client fetching at the same moment cannot fail us on the ref lock.
      for i in 1 2 3; do
        git fetch -q --refmap= origin main \
          && { git rebase -q --autostash FETCH_HEAD || { git rebase --abort; false; }; } \
          && git push -q origin HEAD:main && break
        echo "git sync attempt $i failed, retrying"; sleep 30
      done
    fi
  fi
} >> "$LOG_DIR/collector.log" 2>&1
