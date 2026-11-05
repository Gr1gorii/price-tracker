#!/usr/bin/env bash
# One tick of the server runner (systemd timer, hourly): shops with `runner: server`.
# Pulls first (config changes reach the server), collects inside slot windows, pushes the data.
# Push access: a repo deploy key set as the checkout's core.sshCommand (see README).
set -u
cd "$(dirname "$0")/.."
[[ -f .env ]] && { set -a; source ./.env; set +a; }   # BFP_CONTACT_EMAIL, TELEGRAM_*

# Fetch into FETCH_HEAD only (--refmap=), rebase, push; retries cover network hiccups and
# the other runners pushing at the same moment. `sync pull` skips the push.
sync() {
  for i in 1 2 3; do
    git fetch -q --refmap= origin main \
      && { git rebase -q --autostash FETCH_HEAD || { git rebase --abort; false; }; } \
      && { [[ "${1:-}" == pull ]] || git push -q origin HEAD:main; } && return 0
    echo "git sync attempt $i failed, retrying"; sleep 30
  done
  return 1
}

echo "=== $(date '+%F %T %Z') ==="
sync pull
uv sync -q --frozen --no-dev
out=$(uv run -q --no-sync bfp collect --runner server --scheduled 2>&1); rc=$?
echo "$out"
if [[ $rc -eq 0 && "$out" != nothing\ to\ do* ]]; then
  uv run -q --no-sync bfp health --alert
  git add data/observations data/health data/state
  git diff --cached --quiet || git commit -q -m "data: server run $(date '+%F %H:%M')"
  sync
fi
