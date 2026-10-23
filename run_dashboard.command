#!/bin/zsh
# v2 launcher: one coordinated run (collect -> capture -> snapshot -> files/charts -> email/NTFY/upload).
# Usage: run_dashboard.command [TRIGGER]  (default "scheduled", as launchd passes it: gated by SCHEDULED_RUNS and the NYSE calendar; use "manual" for a hand run, which also opens the dashboard)
# Exit status is the run's own: 0 clean, 3 finished with warnings, 75 another run holds the lock, 1 failed.
# The dashboard opens only for a trigger other than "scheduled", and only when the exit status is 0 or 3.

cd "$(dirname "$0")"
# The project's own virtualenv (setup.sh) when there is one; otherwise the conda base env this script used before.
if [ -x .venv/bin/python ]; then
    export PATH="$PWD/.venv/bin:$PATH"
else
    source ~/.zshrc
    conda activate base
fi

TRIGGER="${1:-scheduled}"

echo "======================================"
echo "  VLADHQ DUAL-HORIZON DASHBOARD (v2)"
echo "  Trigger: $TRIGGER   $(date)"
echo "======================================"

# Optional healthchecks.io dead-man's switch for the three scheduled runs (launchd slots 8:31, 11:30, 14:45).
# One check per slot; the ping URLs are HC_PING_AM / HC_PING_MID / HC_PING_PM in .env. Silent no-op without them.
hc() {  # hc [fail]
    [ "$TRIGGER" = "scheduled" ] || return 0
    case $(date +%H) in 0[0-9]|10) K=HC_PING_AM ;; 11|12) K=HC_PING_MID ;; *) K=HC_PING_PM ;; esac
    U=$(sed -nE "s/^$K=//p" .env 2>/dev/null | tail -1)
    [ -n "$U" ] && curl -fsS -m10 --retry 2 -o /dev/null "$U${1:+/$1}" >/dev/null 2>&1
    return 0
}

hc start
python main_pipeline.py run --trigger "$TRIGGER"
STATUS=$?
case $STATUS in 0|3) hc ;; 75) ;; *) hc fail ;; esac

case $STATUS in
    0) ;;
    3) echo "⚠️  Run finished with warnings (exit 3): see 'python main_pipeline.py status'." ;;
    75) echo "⏳ Another run is already in progress; nothing was started (exit 75)." ;;
    *) echo "❌ Run failed (exit $STATUS)." ;;
esac

if [ "$TRIGGER" != "scheduled" ] && { [ $STATUS -eq 0 ] || [ $STATUS -eq 3 ]; }; then
    DAILY_DIR="$(python -c 'from config import daily_dir; print(daily_dir())')"
    if [ -f "$DAILY_DIR/volume_dashboard.html" ]; then
        echo "\n✅ LAUNCHING DASHBOARD: $DAILY_DIR/volume_dashboard.html"
        open "$DAILY_DIR/volume_dashboard.html"
    fi
fi
exit $STATUS
