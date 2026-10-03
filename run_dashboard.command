#!/bin/zsh
# v2 launcher: one coordinated run (collect -> capture -> snapshot -> files/charts -> email/NTFY/upload).
# Rollback: `git checkout main` in this folder restores the legacy launcher.

source ~/.zshrc
conda activate base
cd "$(dirname "$0")"

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
if [ -f "$DAILY_DIR/volume_dashboard.html" ]; then
    echo "\n✅ LAUNCHING DASHBOARD: $DAILY_DIR/volume_dashboard.html"
    open "$DAILY_DIR/volume_dashboard.html"
fi
exit $STATUS
