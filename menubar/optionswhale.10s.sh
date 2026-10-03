#!/bin/zsh
# <swiftbar.title>Options Whale</swiftbar.title>
# <swiftbar.desc>Status and start/stop for the options_whale api_router (launchd com.vlad.optionswhale)</swiftbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
#
# Installed by ../setup.sh. This folder is SwiftBar's plugin directory: keep only plugins in it.

SELF="${0:A}"                       # resolved, so it also works when symlinked into another plugin folder
ROOT="${SELF:h:h}"
LABEL="com.vlad.optionswhale"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOG="$HOME/Library/Logs/optionswhale.log"
PORT=$(sed -n 's/^OPTIONS_WHALE_PORT=\([0-9][0-9]*\).*/\1/p' "$ROOT/.env" 2>/dev/null | tail -1)
PORT="${PORT:-8080}"

case "$1" in
  start)
    launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || launchctl kickstart "$DOMAIN/$LABEL"
    exit 0 ;;
  stop)
    # bootout, not kill: KeepAlive would restart a killed process. The plist stays, so it starts again at next login.
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
    exit 0 ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    exit 0 ;;
esac

PID=$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/^\tpid = /{print $3}')
HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$PORT/")

if [[ "$HTTP" == "200" ]]; then
  echo "🐋 | sfcolor=#34C759 sfimage=circle.fill"
  STATUS="Online — HTTP 200 (pid $PID)"
elif [[ -n "$PID" ]]; then
  echo "🐋 | sfimage=circle.dotted"
  STATUS="Starting / not responding (pid $PID, HTTP $HTTP)"
else
  echo "🐋 | sfcolor=#FF3B30 sfimage=circle"
  STATUS="Offline"
fi

echo "---"
echo "Options Whale: $STATUS"
echo "Port $PORT | color=gray"
echo "---"
if [[ ! -f "$PLIST" ]]; then
  echo "Not installed — run setup.sh | color=gray"
elif [[ -n "$PID" ]]; then
  echo "Stop server | bash=\"$SELF\" param1=stop terminal=false refresh=true sfimage=stop.fill"
  echo "Restart server | bash=\"$SELF\" param1=restart terminal=false refresh=true sfimage=arrow.clockwise"
else
  echo "Start server | bash=\"$SELF\" param1=start terminal=false refresh=true sfimage=play.fill"
fi
echo "---"
echo "Open in browser | href=http://localhost:$PORT sfimage=safari"
echo "Open log | bash=/usr/bin/open param1=-a param2=Console param3=\"$LOG\" terminal=false sfimage=doc.text"
echo "Refresh | refresh=true"
