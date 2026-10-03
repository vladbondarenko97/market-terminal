#!/bin/bash
# One-shot setup for a new Mac: Python env, .env, the options_whale server as a login service,
# and the menu bar control. Safe to re-run; it only fills in what is missing.
#
#   ./setup.sh               full setup
#   ./setup.sh --no-menubar  skip SwiftBar / the menu bar icon
#   ./setup.sh --uninstall   stop the server and remove the login service (keeps .env, .venv and data)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.vlad.optionswhale"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOG="$HOME/Library/Logs/optionswhale.log"
PLUGIN_DIR="$ROOT/menubar"
PLUGIN="$PLUGIN_DIR/optionswhale.10s.sh"
SWIFTBAR_ID="com.ameba.SwiftBar"

MENUBAR=1
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --no-menubar) MENUBAR=0 ;;
        --uninstall)  UNINSTALL=1 ;;
        -h|--help)    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*" >&2; }
die()  { printf 'xx  %s\n' "$*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || die "setup.sh supports macOS only."

if [ "$UNINSTALL" = 1 ]; then
    step "Removing the login service"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    if [ "$(defaults read "$SWIFTBAR_ID" PluginDirectory 2>/dev/null || true)" = "$PLUGIN_DIR" ]; then
        defaults delete "$SWIFTBAR_ID" PluginDirectory
        osascript -e 'quit app "SwiftBar"' 2>/dev/null || true
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
PORT="$(sed -n 's/^OPTIONS_WHALE_PORT=\([0-9][0-9]*\).*/\1/p' "$ROOT/.env" | tail -1)"
PORT="${PORT:-8080}"
echo "data: $DATA_DIR"
echo "port: $PORT"
[ -f "$DATA_DIR/portfolio.db" ] || warn "No portfolio.db in $DATA_DIR yet. Copy CME_Data from another Mac, or run the pipeline to create it."

HAVE_KEY="$(cd "$ROOT" && "$VPY" -c 'from config import DATABENTO_API_KEY as k; print(1 if k else 0)')"

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
if [ "$HAVE_KEY" = 1 ]; then
    # bootout returns before the old process is fully gone; retry the bootstrap briefly.
    for _ in 1 2 3 4 5; do launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break; sleep 1; done
    for _ in $(seq 1 45); do
        if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$PORT/")" = "200" ]; then SERVER_UP=1; break; fi
        sleep 1
    done
    if [ "$SERVER_UP" = 1 ]; then echo "ok: http://localhost:$PORT"; else warn "Server did not answer on port $PORT. See $LOG"; fi
else
    warn "DATABENTO_API_KEY is empty in .env, so the server was installed but not started."
fi

# --- 5. Menu bar icon -----------------------------------------------------------------------
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
elif [ "$HAVE_KEY" = 1 ]; then
    echo "Server:   installed but NOT responding. Check $LOG"
else
    echo "Server:   NOT started. Fill in $ROOT/.env (at least DATABENTO_API_KEY), then run ./setup.sh again."
fi
[ "$MENUBAR" = 1 ] && echo "Menu bar: whale icon, top right (status, start, stop, restart)"
echo "Log:      $LOG"
[ "$SERVER_UP" = 1 ] || exit 1
