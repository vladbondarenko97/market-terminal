#!/bin/bash
# Deploy origin/main to this machine (the live terminal and the MCP server), only when CI is green for that commit.
#   scripts/deploy.sh            deploy now if there is something new and safe
#   scripts/deploy.sh --check    say what it would do, change nothing
#   scripts/deploy.sh --auto     what com.vladhq.deploy (~/dev/agents/mcp/deploy.sh) runs every minute: the same, plus ntfy
#                                (once per situation), and while the Mac serves market.vlad.yt (_placement.vlad.yt TXT,
#                                market=mac) Oracle, the standby, must run the commit first; if it has not within an hour it
#                                probably rolled it back there, and the Mac holds. An Oracle that does not answer holds nothing.
# Safe to run from a timer: it exits quietly when there is nothing to do, waits while any main_pipeline.py job runs (new
# code must not land under a run, a bars job or a backfill), never starts a run, sends nothing but ntfy and never touches
# .env. If the services do not answer after a restart it puts the old code back. Its twin on Oracle is infra's
# oracle/market-deploy.sh. docs/operations.md#deploying-to-the-live-machine
set -euo pipefail
MODE=${1:-}
if [ -z "${DEPLOY_REPO:-}" ]; then          # run from a copy: the fast-forward below rewrites this file, and bash reads as it goes
  copy=$(mktemp); cp "$0" "$copy"
  DEPLOY_REPO=$(cd "$(dirname "$0")/.." && pwd) exec bash "$copy" "$@"
fi
trap 'rm -f "$0"' EXIT
cd "$DEPLOY_REPO"
SERVICES=(com.vlad.optionswhale com.vlad.mcpserver)
GITDIR=$(git rev-parse --git-dir)
STAMP="$GITDIR/deployed-sha"    # the commit the services were last restarted on (a hand-run `git pull` does not move it)
BAD="$GITDIR/deployed-sha.bad"  # a commit that failed its health check: not retried until origin/main moves on
say() { echo "deploy: $*"; }
TOPIC=""
[ "$MODE" = --auto ] && TOPIC=$(sed -n 's/^NTFY_TOPIC=//p' "$HOME/dev/infra/monitoring/.env" 2>/dev/null | tr -d "'\"" || true)
note() {                                     # $1 situation key, $2 title, $3 text: ntfy once per situation (--auto only)
  say "$2: $3"
  [ -n "$TOPIC" ] || return 0
  [ "$(cat "$GITDIR/deploy-notified" 2>/dev/null)" = "$1" ] && return 0
  echo "$1" > "$GITDIR/deploy-notified"
  curl -s -m 10 -H "Title: market: $2" -d "$3" "https://ntfy.sh/$TOPIC" >/dev/null || true
}
since() { [ -f "$GITDIR/$1" ] || date +%s > "$GITDIR/$1"; echo $(( $(date +%s) - $(cat "$GITDIR/$1") )); }   # seconds since first seen

mkdir "$GITDIR/deploy.lock" 2>/dev/null || {                       # one at a time; a lock older than 30 minutes is a dead run's
  [ -n "$(find "$GITDIR/deploy.lock" -maxdepth 0 -mmin +30 2>/dev/null)" ] && rmdir "$GITDIR/deploy.lock"
  say "another deploy is running"; exit 0; }
trap 'rm -f "$0"; rmdir "$GITDIR/deploy.lock" 2>/dev/null || true' EXIT

git fetch -q origin main
OLD=$(git rev-parse HEAD); NEW=$(git rev-parse origin/main)
[ -f "$STAMP" ] || echo "$OLD" > "$STAMP"           # first run: assume the services run what is checked out
if [ "$OLD" = "$NEW" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$NEW" ]; then
  rm -f "$GITDIR/deploy-busy-since" "$GITDIR/deploy-canary-since"
  say "up to date ($(git rev-parse --short HEAD))"; exit 0
fi
SHORT=$(git rev-parse --short "$NEW")
[ "$(git branch --show-current)" = main ] || { note "branch $NEW" "deploy blocked" "the live checkout is not on main: not touching it"; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { note "dirty $NEW" "deploy blocked" "uncommitted changes in the live checkout: not deploying $SHORT"; exit 1; }
git merge-base --is-ancestor "$OLD" "$NEW" || { note "diverged $NEW" "deploy blocked" "local main is not behind origin/main (diverged or ahead): not touching it"; exit 1; }
LIVE=$(cat "$STAMP")                                 # what the services run; roll back to this
{ git cat-file -e "$LIVE^{commit}" 2>/dev/null && git merge-base --is-ancestor "$LIVE" "$NEW"; } || LIVE=$OLD   # stamp gone after a history rewrite
[ "$(cat "$BAD" 2>/dev/null)" = "$NEW" ] && { say "$SHORT already failed its health check: waiting for a newer commit"; exit 1; }

# CI must have passed for exactly this commit (the latest "test" run on it)
CI=$(gh run list --workflow test --commit "$NEW" --limit 1 --json status,conclusion -q '.[0] | "\(.status)/\(.conclusion)"' 2>/dev/null || true)
case "$CI" in
  completed/success) ;;
  completed/*) note "cifail $NEW" "CI failed, not deploying" "$(git log -1 --format='%h %s' "$NEW")"; exit 1 ;;
  *) say "CI for $SHORT is '${CI:-none}', not green: waiting"; exit 1 ;;
esac

if pgrep -f main_pipeline.py >/dev/null; then
  [ "$(since deploy-busy-since)" -ge 10800 ] && note "busy $NEW" "deploy waiting" "a main_pipeline.py job has run for 3 h; $SHORT waits for it"
  say "a main_pipeline.py job is running: waiting"; exit 1
fi
rm -f "$GITDIR/deploy-busy-since"

if [ "$MODE" = --auto ]; then                        # placement: when the Mac serves market.vlad.yt, Oracle goes first
  PREF=$(dig +short +time=3 +tries=1 TXT _placement.vlad.yt 2>/dev/null | tr -d '"' | tr ' ' '\n' | sed -n 's/^market=//p' | head -1 || true)
  if [ "${PREF:-mac}" = mac ]; then
    PEER=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "${DEPLOY_ORACLE:-ubuntu@brinewater}" "curl -s -m 5 'http://127.0.0.1:8792/api/version?peer=0'" 2>/dev/null \
           | python3 -c 'import json,sys; print(json.load(sys.stdin).get("commit") or "")' 2>/dev/null || true)
    if [ -n "$PEER" ] && [ "$PEER" != "$NEW" ]; then
      [ "$(since deploy-canary-since)" -ge "${DEPLOY_WAIT_CANARY:-3600}" ] && \
        note "canary $NEW" "deploy held" "Oracle has not taken $SHORT within an hour (rolled back there?); the Mac stays on $(git rev-parse --short "$LIVE")"
      say "waiting for Oracle (the standby) to run $SHORT first"; exit 1
    fi
    [ -z "$PEER" ] && say "Oracle's version did not answer: deploying on the Mac's own checks"
  fi
fi
rm -f "$GITDIR/deploy-canary-since"

say "$(git rev-parse --short "$LIVE") -> $SHORT, CI green"
git diff --stat "$LIVE" "$NEW" | tail -1
[ "$MODE" = "--check" ] && exit 0

PORT=$(MT_NO_KEYCHAIN=1 .venv/bin/python -c "import config; print(config.OPTIONS_WHALE_PORT)" 2>/dev/null || echo 80)
MCP_PORT=$(MT_NO_KEYCHAIN=1 .venv/bin/python -c "import config; print(config.int_env('MCP_PORT', 8787))" 2>/dev/null || echo 8787)
loaded() { launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1; }      # a job switched off on the dash stays off
ok() { curl -sf -m 10 -o /dev/null "$1"; }
healthy() {                                          # $1: the commit /api/version must report (none reported: older code)
  local u v
  if loaded com.vlad.optionswhale; then
    for _ in $(seq 30); do ok "http://127.0.0.1:$PORT/help" && break; sleep 2; done
    for u in / /help /api/positions "/api/forecast?ticker=SPY"; do ok "http://127.0.0.1:$PORT$u" || { echo "terminal $u"; return 1; }; done
    v=$(curl -s -m 10 "http://127.0.0.1:$PORT/api/version?peer=0" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("commit") or "")' 2>/dev/null || true)
    [ -z "$v" ] || [ "$v" = "$1" ] || { echo "the terminal runs ${v:0:7}, not ${1:0:7}"; return 1; }
  fi
  if loaded com.vlad.mcpserver; then
    for _ in $(seq 15); do ok "http://127.0.0.1:$MCP_PORT/healthz" && return 0; sleep 2; done
    echo "MCP /healthz"; return 1
  fi
}
restart() { for s in "${SERVICES[@]}"; do if loaded "$s"; then launchctl kickstart -k "gui/$(id -u)/$s" || say "could not restart $s"; fi; done; }
install() {                                          # $1 from, $2 to: dependencies, byte-compile, restart, health check
  git diff --quiet "$1" "$2" -- requirements.txt || .venv/bin/pip install -q -r requirements.txt || { echo "pip install"; return 1; }
  if [ -x .venv/bin/python ]; then .venv/bin/python -m compileall -q core options_whale ./*.py >/dev/null || { echo "byte-compile"; return 1; }; fi
  restart
  healthy "$2"
}

git merge --ff-only -q "$NEW"
if why=$(install "$LIVE" "$NEW"); then
  echo "$NEW" > "$STAMP"; rm -f "$BAD"
  note "deployed $NEW" "deployed $SHORT" "$(git log -1 --format=%s "$NEW")"
else
  say "${why:-the check} failed after the restart: rolling back to $(git rev-parse --short "$LIVE")"
  echo "$NEW" > "$BAD"
  git reset -q --hard "$LIVE"        # safe: the tree was clean and the new commits are on origin
  if again=$(install "$NEW" "$LIVE"); then r="rolled back, terminal answers"; else r="STILL DOWN (${again:-?}): check ~/Library/Logs"; fi
  note "rolledback $NEW" "deploy $SHORT rolled back" "${why:-the check} failed; $r"
  exit 1
fi
