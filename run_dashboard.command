#!/bin/zsh
# v2 launcher: one coordinated run (collect -> capture -> snapshot -> files/charts -> email/NTFY/upload).
# Rollback: `git checkout main` in this folder restores the legacy launcher.

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

python main_pipeline.py run --trigger "$TRIGGER"
STATUS=$?

if [ $STATUS -eq 75 ]; then
    echo "⏳ Another run is already in progress; this request observed it and exited."
    exit 0
fi

DAILY_DIR="$(python -c 'from config import daily_dir; print(daily_dir())')"
if [ "$TRIGGER" != "scheduled" ] && [ -f "$DAILY_DIR/volume_dashboard.html" ]; then
    echo "\n✅ LAUNCHING DASHBOARD: $DAILY_DIR/volume_dashboard.html"
    open "$DAILY_DIR/volume_dashboard.html"
fi
exit $STATUS
