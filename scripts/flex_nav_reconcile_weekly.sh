#!/bin/bash
# Weekly LIVE equity_history <-> Flex NAV reconciliation (task 5, 2026-10-14).
# Runs in WSL: copies the Flex token into the live container, then runs the
# reconcile script (ONE Flex window per run, flag-only by default).
# Add to WSL crontab: 0 9 * * 1  /mnt/d/quant/scripts/flex_nav_reconcile_weekly.sh
set -euo pipefail
rc=0

TOKEN_SRC="/mnt/c/Users/Cap/Downloads/token.txt"
CONTAINER="quant-dashboard-live-docker"
LOG="/home/cap/flex_nav_reconcile.log"

echo "===== $(date -Is) weekly Flex NAV reconcile =====" >> "$LOG"

if [ ! -f "$TOKEN_SRC" ]; then
    echo "FATAL: Flex token file missing: $TOKEN_SRC (Downloads cleaned? re-download)" >> "$LOG"
    exit 1
fi

docker cp "$TOKEN_SRC" "$CONTAINER:/tmp/flex_token.txt"
docker cp "/mnt/d/quant/scripts/flex_nav_reconcile.py" "$CONTAINER:/tmp/flex_nav_reconcile.py"
docker exec -e PYTHONPATH=/app "$CONTAINER" /app/.venv/bin/python3 \
    /tmp/flex_nav_reconcile.py --token-file /tmp/flex_token.txt --days 14 \
    >> "$LOG" 2>&1 || rc=$?
# rc: 0 ok, 1 fetch/parse failure, 2 statement shows an UNINTENDED SHORT (alarm already
# pushed RED from inside the container). `|| rc=$?` keeps set -e from skipping the token
# cleanup below on a non-zero exit (2026-11-21).
docker exec "$CONTAINER" rm -f /tmp/flex_token.txt /tmp/flex_nav_reconcile.py || true
echo "exit=$rc" >> "$LOG"
exit $rc
