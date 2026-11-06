#!/bin/zsh
# Local collector run (launchd fires it hourly at :01; the gate makes it collect once per slot).
# Optional: BFP_GIT_SYNC=1 to commit & push collected data.
set -u
# Keep the Mac awake for the whole tick: after a scheduled wake (pmset, 08:01 / 20:01) it would
# fall back asleep within seconds. -i: no idle sleep; -s: no system sleep on AC power (DarkWake
# included). A closed lid still sleeps.
[[ -z "${BFP_AWAKE:-}" ]] && exec /usr/bin/env BFP_AWAKE=1 /usr/bin/caffeinate -is /bin/zsh "${0:A}" "$@"
cd "${0:A:h}/.."
[[ -f .env ]] && { set -a; source ./.env; set +a; }   # BFP_CONTACT_EMAIL, BFP_GIT_SYNC, TELEGRAM_*
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
LOG_DIR="$HOME/Library/Logs/bfp"; mkdir -p "$LOG_DIR"
{
  echo "=== $(date '+%F %T %Z') ==="
  # Backup trigger for the GitHub runner (its cron is sometimes late or dropped): queue the
  # workflow once per new slot; its own gate makes this a no-op if the slot is already done.
  if uv run bfp gate --runner local 2>/dev/null | grep -q '^run=true'; then
    # Wi-Fi takes a few seconds to come back after a wake, and a run started offline would find
    # robots.txt unreachable and close the slot with no data: wait up to ~2 min, else skip the tick.
    online=0
    for i in {1..12}; do
      curl -s -o /dev/null --max-time 10 https://github.com && { online=1; break; }
      sleep 10
    done
    (( online )) || { echo "offline, tick skipped"; exit 0; }
    gh workflow run collect.yml >/dev/null 2>&1 && echo "queued GitHub collect (backup trigger)"
  fi
  out=$(uv run bfp collect --runner local --scheduled 2>&1); rc=$?
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
