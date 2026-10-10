#!/bin/bash
# One-shot setup for a new Mac: Python env, .env, the options_whale server as a login service,
# and the menu bar control. Safe to re-run; it only fills in what is missing.
#
#   ./setup.sh                    full setup (no schedule)
#   ./setup.sh --schedule         also make THIS Mac the one that runs the pipeline every NYSE trading day
#                                 at the open (09:31 ET) and before the close (15:45 ET; not on 13:00 ET early closes)
#   ./setup.sh --remove-schedule  stop scheduled runs on this Mac (run it on every Mac except the scheduler)
#   ./setup.sh --no-menubar       skip SwiftBar / the menu bar icon
#   ./setup.sh --uninstall        stop the server and the schedule, remove both services and the menu bar link (keeps
#                                 .env, .venv, data; only SCHEDULED_RUNS in .env is cleared)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.vlad.optionswhale"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOG="$HOME/Library/Logs/optionswhale.log"
PLUGIN_DIR="$ROOT/menubar"
PLUGIN="$PLUGIN_DIR/optionswhale.10s.sh"
SWIFTBAR_ID="com.ameba.SwiftBar"
SCHED_LABEL="com.vlad.marketdashboard"
SCHED_PLIST="$HOME/Library/LaunchAgents/$SCHED_LABEL.plist"
SCHED_LOG="$HOME/Library/Logs/marketdashboard.log"
ALERT_LABEL="com.vlad.signalalerts"
ALERT_PLIST="$HOME/Library/LaunchAgents/$ALERT_LABEL.plist"
ALERT_LOG="$HOME/Library/Logs/signalalerts.log"

MENUBAR=1
UNINSTALL=0
SCHEDULE=0
REMOVE_SCHEDULE=0
for arg in "$@"; do
    case "$arg" in
        --no-menubar) MENUBAR=0 ;;
        --uninstall)  UNINSTALL=1 ;;
        --schedule)   SCHEDULE=1 ;;
        --remove-schedule) REMOVE_SCHEDULE=1 ;;
        -h|--help)    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*" >&2; }
die()  { printf 'xx  %s\n' "$*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || die "setup.sh supports macOS only."
[ "$SCHEDULE" = 1 ] && [ "$REMOVE_SCHEDULE" = 1 ] && die "--schedule and --remove-schedule contradict each other."

# SCHEDULED_RUNS in .env is the second lock: a leftover launchd job on another Mac still skips without it.
set_scheduled_runs() {
    [ -f "$ROOT/.env" ] || return 0
    if grep -q '^SCHEDULED_RUNS=' "$ROOT/.env"; then
        sed -i '' "s/^SCHEDULED_RUNS=.*/SCHEDULED_RUNS=$1/" "$ROOT/.env"
    else
        printf '\nSCHEDULED_RUNS=%s\n' "$1" >> "$ROOT/.env"
    fi
}

remove_schedule() {
    launchctl bootout "$DOMAIN/$SCHED_LABEL" 2>/dev/null || true
    launchctl bootout "$DOMAIN/$ALERT_LABEL" 2>/dev/null || true
    rm -f "$SCHED_PLIST" "$ALERT_PLIST"
    set_scheduled_runs ""
}

if [ "$REMOVE_SCHEDULE" = 1 ]; then
    step "Removing the pipeline schedule ($SCHED_LABEL)"
    remove_schedule
    echo "Done. This Mac no longer runs the pipeline on a schedule; manual runs still work."
    exit 0
fi

if [ "$UNINSTALL" = 1 ]; then
    step "Removing the login service and the pipeline schedule"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    remove_schedule
    CURRENT_DIR="$(defaults read "$SWIFTBAR_ID" PluginDirectory 2>/dev/null || true)"
    LINK="$CURRENT_DIR/$(basename "$PLUGIN")"
    if [ "$CURRENT_DIR" = "$PLUGIN_DIR" ]; then
        defaults delete "$SWIFTBAR_ID" PluginDirectory
        osascript -e 'quit app "SwiftBar"' 2>/dev/null || true
    elif [ -n "$CURRENT_DIR" ] && [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$PLUGIN" ]; then
        # Step 6 linked the plugin into SwiftBar's own folder; remove only a link that points at this repository.
        rm -f "$LINK"
        echo "Removed the menu bar plugin link $LINK"
    fi
    echo "Done. .env, .venv and the data folder were left in place; SwiftBar itself was not uninstalled."
    exit 0
fi

# --- 1. Homebrew + Python -------------------------------------------------------------------
step "Homebrew"
find_brew() {
    command -v brew 2>/dev/null && return
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && echo "$b" && return; done
    return 1
}
if ! BREW="$(find_brew)"; then
    echo "Homebrew not found; installing it (asks for your Mac password)."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    BREW="$(find_brew)" || die "Homebrew install did not finish."
fi
eval "$("$BREW" shellenv)"
echo "ok: $BREW"

step "Python"
PY="$(brew --prefix)/bin/python3"
[ -x "$PY" ] || brew install python
[ -x "$PY" ] || die "python3 not found after 'brew install python'."
echo "ok: $("$PY" --version)"

# --- 2. Virtualenv + packages ---------------------------------------------------------------
step "Python packages (.venv)"
[ -x "$ROOT/.venv/bin/python" ] || "$PY" -m venv "$ROOT/.venv"
VPY="$ROOT/.venv/bin/python"
"$VPY" -m pip install -q --upgrade pip
"$VPY" -m pip install -q -r "$ROOT/requirements.txt"
"$VPY" -m playwright install chromium >/dev/null
echo "ok"

# --- 3. .env + data folder ------------------------------------------------------------------
step "Settings (.env) and data folder"
if [ ! -f "$ROOT/.env" ]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    echo "Created .env from .env.example."
fi
chmod 600 "$ROOT/.env"

# An explicit PORTFOLIO_DATA_DIR copied from another Mac may point at a folder that isn't here.
DATA_DIR="$(cd "$ROOT" && "$VPY" -c 'from config import DATA_DIR; print(DATA_DIR)')"
mkdir -p "$DATA_DIR" 2>/dev/null || die "Cannot create data folder $DATA_DIR. Fix PORTFOLIO_DATA_DIR in .env and re-run."
# Same reading as the menu bar plugin: a blank or missing OPTIONS_WHALE_PORT means 8080 (config.py does the same).
PORT="$(sed -nE "s/^(export +)?OPTIONS_WHALE_PORT *= *[\"']?([0-9]+).*/\2/p" "$ROOT/.env" | tail -1 || true)"
PORT="${PORT:-8080}"
echo "data: $DATA_DIR"
echo "port: $PORT"
[ -f "$DATA_DIR/portfolio.db" ] || warn "No portfolio.db in $DATA_DIR yet. Copy CME_Data from another Mac, or run the pipeline to create it."

HAVE_KEY="$(cd "$ROOT" && "$VPY" -c 'from config import DATABENTO_API_KEY as k; print(1 if k else 0)')"
[ "$HAVE_KEY" = 1 ] || warn "DATABENTO_API_KEY is empty in .env: the server starts, but the dark pool panels stay empty."

# --- 4. Server as a login service -----------------------------------------------------------
step "Login service ($LABEL)"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VPY</string>
        <string>api_router.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT/options_whale</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
SERVER_UP=0
# bootout returns before the old process is fully gone; retry the bootstrap briefly.
for _ in 1 2 3 4 5; do launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break; sleep 1; done
for _ in $(seq 1 45); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$PORT/")" = "200" ]; then SERVER_UP=1; break; fi
    sleep 1
done
if [ "$SERVER_UP" = 1 ]; then echo "ok: http://localhost:$PORT"; else warn "Server did not answer on port $PORT. See $LOG"; fi

# --- 5. Pipeline schedule (only on the one Mac that is the source of truth) --------------------
if [ "$SCHEDULE" = 1 ]; then
    step "Pipeline schedule ($SCHED_LABEL)"
    # launchd uses this Mac's clock. core/market_calendar.py lists every local fire time (weekday hour minute) that
    # covers the 09:31 and 15:45 ET slots on each NYSE trading day of the coming year, so the job stays right through
    # every daylight-saving change in any time zone. A fire on the wrong side of a clock change skips itself (the
    # run's own gate), so having both clock times of a zone installed is harmless.
    TIMES="$(cd "$ROOT" && "$VPY" -c '
from core.market_calendar import launchd_intervals
for weekday, hour, minute in launchd_intervals():
    print(weekday, hour, minute)
')"
    [ -n "$TIMES" ] || die "Could not work out the schedule times (core/market_calendar.py)."
    INTERVALS=""
    while read -r WD H M; do
        INTERVALS="$INTERVALS        <dict><key>Weekday</key><integer>$WD</integer><key>Hour</key><integer>$H</integer><key>Minute</key><integer>$M</integer></dict>
"
    done <<< "$TIMES"
    cat > "$SCHED_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$SCHED_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>$ROOT/run_dashboard.command</string>
        <string>scheduled</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>StartCalendarInterval</key>
    <array>
$INTERVALS    </array>
    <key>StandardOutPath</key>
    <string>$SCHED_LOG</string>
    <key>StandardErrorPath</key>
    <string>$SCHED_LOG</string>
</dict>
</plist>
EOF
    plutil -lint "$SCHED_PLIST" >/dev/null
    set_scheduled_runs 1
    launchctl bootout "$DOMAIN/$SCHED_LABEL" 2>/dev/null || true
    for _ in 1 2 3 4 5; do launchctl bootstrap "$DOMAIN" "$SCHED_PLIST" 2>/dev/null && break; sleep 1; done
    launchctl print "$DOMAIN/$SCHED_LABEL" >/dev/null 2>&1 || warn "launchd did not load $SCHED_PLIST"
    LOCAL_TIMES="$(echo "$TIMES" | awk '{printf "%02d:%02d\n", $2, $3}' | sort -u | tr '\n' ' ')"
    echo "ok: $(echo "$TIMES" | wc -l | tr -d ' ') launch times, local clock times ${LOCAL_TIMES% }."
    echo "    A fire runs only at 09:31, 12:30 or 15:45 ET (up to 30 minutes late) on an NYSE trading day; 13:00 ET early closes skip the 15:45 run."
    # Signal Watch alerts: every 15 minutes of the regular session (core/watch.ALERT_SLOTS_ET), on weekdays only. The
    # command's own session gate skips holidays and a fire on the wrong side of a clock change.
    ALERT_INTERVALS="$(cd "$ROOT" && "$VPY" -c '
from core.market_calendar import launchd_intervals
from core.watch import ALERT_SLOTS_ET
for weekday, hour, minute in launchd_intervals(slots=ALERT_SLOTS_ET):
    print(f"        <dict><key>Weekday</key><integer>{weekday}</integer><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>")
')"
    cat > "$ALERT_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$ALERT_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VPY</string>
        <string>$ROOT/main_pipeline.py</string>
        <string>signal-alerts</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>StartCalendarInterval</key>
    <array>
$ALERT_INTERVALS
    </array>
    <key>StandardOutPath</key>
    <string>$ALERT_LOG</string>
    <key>StandardErrorPath</key>
    <string>$ALERT_LOG</string>
</dict>
</plist>
EOF
    plutil -lint "$ALERT_PLIST" >/dev/null
    launchctl bootout "$DOMAIN/$ALERT_LABEL" 2>/dev/null || true
    for _ in 1 2 3 4 5; do launchctl bootstrap "$DOMAIN" "$ALERT_PLIST" 2>/dev/null && break; sleep 1; done
    launchctl print "$DOMAIN/$ALERT_LABEL" >/dev/null 2>&1 || warn "launchd did not load $ALERT_PLIST"
    echo "ok: Signal Watch alerts every 15 minutes of the session, weekdays only (log: $ALERT_LOG)"
    # A sleeping Mac runs the missed job when it wakes; the run skips itself if that is more than 30 minutes late.
    SLEEP_MIN="$(pmset -g 2>/dev/null | awk '$1=="sleep"{print $2; exit}')"
    if [ -n "$SLEEP_MIN" ] && [ "$SLEEP_MIN" != "0" ]; then
        warn "This Mac sleeps after $SLEEP_MIN min idle and would miss runs. Turn on System Settings > Energy >"
        warn "'Prevent automatic sleeping when the display is off', or run: sudo pmset -a sleep 0"
    fi
fi

# --- 6. Menu bar icon -----------------------------------------------------------------------
if [ "$MENUBAR" = 1 ]; then
    step "Menu bar icon (SwiftBar)"
    chmod +x "$PLUGIN"
    [ -d "/Applications/SwiftBar.app" ] || brew install --cask swiftbar
    CURRENT_DIR="$(defaults read "$SWIFTBAR_ID" PluginDirectory 2>/dev/null || true)"
    if [ -n "$CURRENT_DIR" ] && [ "$CURRENT_DIR" != "$PLUGIN_DIR" ] && [ -d "$CURRENT_DIR" ]; then
        # SwiftBar is already in use with its own plugin folder: add ours to it instead of taking over.
        ln -sf "$PLUGIN" "$CURRENT_DIR/$(basename "$PLUGIN")"
        echo "Linked into existing SwiftBar folder: $CURRENT_DIR"
    else
        defaults write "$SWIFTBAR_ID" PluginDirectory -string "$PLUGIN_DIR"
    fi
    if ! osascript -e 'tell application "System Events" to get the name of every login item' 2>/dev/null | grep -q SwiftBar; then
        osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/SwiftBar.app", hidden:true}' >/dev/null 2>&1 \
            || warn "Could not add SwiftBar to Login Items; add it in System Settings > General > Login Items."
    fi
    osascript -e 'quit app "SwiftBar"' 2>/dev/null || true
    sleep 1
    open -a SwiftBar
    echo "ok"
fi

# --- Summary --------------------------------------------------------------------------------
step "Summary"
if [ "$SERVER_UP" = 1 ]; then
    echo "Server:   running at http://localhost:$PORT (starts at login, restarts if it crashes)"
else
    echo "Server:   installed but NOT responding. Check $LOG"
fi
[ "$HAVE_KEY" = 1 ] || echo "Keys:     DATABENTO_API_KEY is empty: fill in $ROOT/.env for the dark pool panels and block flow."
[ "$MENUBAR" = 1 ] && echo "Menu bar: whale icon, top right (status, start, stop, restart)"
if [ "$SCHEDULE" = 1 ]; then
    echo "Schedule: this Mac runs the pipeline at the open and before the close (log: $SCHED_LOG)"
elif [ -f "$SCHED_PLIST" ]; then
    echo "Schedule: a pipeline schedule is installed here; run ./setup.sh --remove-schedule unless this is the scheduler"
fi
echo "Log:      $LOG"
[ "$SERVER_UP" = 1 ] || exit 1
