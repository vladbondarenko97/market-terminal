#!/bin/bash
# Deploy origin/main to this machine (the live terminal and the MCP server), only when CI is green for that commit.
#   scripts/deploy.sh            deploy now if there is something new and safe
#   scripts/deploy.sh --check    say what it would do, change nothing
# Safe to run from a timer: it exits quietly when there is nothing to do. It never starts a pipeline run, sends
# anything or touches .env. If the services do not answer after a restart it puts the old code back.
set -euo pipefail
cd "$(dirname "$0")/.."
SERVICES=(com.vlad.optionswhale com.vlad.mcpserver)
say() { echo "deploy: $*"; }

git fetch -q origin main
OLD=$(git rev-parse HEAD); NEW=$(git rev-parse origin/main)
[ "$OLD" = "$NEW" ] && { say "up to date ($(git rev-parse --short HEAD))"; exit 0; }
[ "$(git branch --show-current)" = main ] || { say "checkout is not on main: not touching it"; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { say "uncommitted changes in the checkout: not touching it"; exit 1; }
git merge-base --is-ancestor "$OLD" "$NEW" || { say "local main is not behind origin/main (diverged or ahead): not touching it"; exit 1; }

# CI must have passed for exactly this commit (the latest "test" run on it)
CI=$(gh run list --workflow test --commit "$NEW" --limit 1 --json status,conclusion -q '.[0] | "\(.status)/\(.conclusion)"' 2>/dev/null || true)
[ "$CI" = "completed/success" ] || { say "CI for $(git rev-parse --short "$NEW") is '${CI:-none}', not green: waiting"; exit 1; }

say "$(git rev-parse --short "$OLD") -> $(git rev-parse --short "$NEW"), CI green"
git diff --stat "$OLD" "$NEW" | tail -1
[ "${1:-}" = "--check" ] && exit 0

PORT=$(MT_NO_KEYCHAIN=1 .venv/bin/python -c "import config; print(config.OPTIONS_WHALE_PORT)" 2>/dev/null || echo 80)
healthy() { for _ in $(seq 20); do curl -sf -m3 "http://127.0.0.1:$PORT/help" >/dev/null && return 0; sleep 1; done; return 1; }
restart() { for s in "${SERVICES[@]}"; do launchctl kickstart -k "gui/$(id -u)/$s" || say "could not restart $s"; done; }

git merge --ff-only -q "$NEW"
git diff --quiet "$OLD" "$NEW" -- requirements.txt || .venv/bin/pip install -q -r requirements.txt
restart
if healthy; then
  say "deployed $(git rev-parse --short HEAD); terminal answers on port $PORT"
else
  say "terminal did not answer after the restart: rolling back to $(git rev-parse --short "$OLD")"
  git reset -q --hard "$OLD"        # safe: the tree was clean and the new commits are on origin
  git diff --quiet "$NEW" "$OLD" -- requirements.txt || .venv/bin/pip install -q -r requirements.txt
  restart; healthy && say "rolled back, terminal answers" || say "STILL DOWN: check ~/Library/Logs"
  exit 1
fi
