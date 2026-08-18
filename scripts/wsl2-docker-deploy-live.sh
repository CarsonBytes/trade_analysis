#!/bin/bash
# LIVE variant of scripts/wsl2-docker-deploy.sh -- same shape, targets docker-compose.live.yml
# under its own compose project name so it never collides with paper's compose run from the
# same synced /home/cap/quant directory.
#
# DELIBERATELY NEVER WIRED INTO ANY GIT HOOK -- unlike paper's script (auto-deployed on every
# push via .git/hooks/pre-push), this stays manually invoked indefinitely unless explicitly
# asked to automate later. An ordinary code push must never trigger a live-account container
# recreate/relogin on its own.
#
# Usage:
#   wsl -d Ubuntu -- bash /mnt/d/quant/scripts/wsl2-docker-deploy-live.sh
set -uo pipefail

LOG="/home/cap/quant-deploy.log"
DASH_URL="http://localhost:18081"
COMPOSE="docker compose -f docker-compose.live.yml -p quant-live"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

{
    echo "=== LIVE deploy started $(ts) (triggered by: ${1:-manual}) ==="

    rsync -a --delete \
        --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
        --exclude='*.pyc' --exclude='*.db' --exclude='logs' \
        --exclude='.env' --exclude='.env.live' \
        /mnt/d/quant/ /home/cap/quant/
    rsync_rc=$?
    if [ $rsync_rc -ne 0 ]; then
        echo "!!! rsync failed (exit $rsync_rc) -- aborting, previous deployment left running"
        echo "=== LIVE deploy FAILED $(ts) ==="
        exit 1
    fi

    cd /home/cap/quant || { echo "!!! /home/cap/quant missing after rsync"; exit 1; }

    # Precondition check probes native LIVE's port 4001 (NOT paper's 4002) -- if it's still
    # reachable, EXISTING_SESSION_DETECTED_ACTION=primary should still resolve the one-
    # session-per-username conflict, but expect an extra login cycle. Informational only,
    # matching paper's own script -- not a hard abort.
    win_ip=$(ip route | awk '/^default/ {print $3; exit}')
    if [ -n "$win_ip" ] && timeout 2 bash -c "cat < /dev/null > /dev/tcp/$win_ip/4001" 2>/dev/null; then
        echo "!!! WARNING: native Windows LIVE gateway (port 4001) appears to still be up"
        echo "    ($win_ip:4001 reachable) -- EXISTING_SESSION_DETECTED_ACTION should still"
        echo "    resolve this, but this WILL kick the native session the moment login"
        echo "    succeeds. Expected during Stage 3 cutover; unexpected any other time."
    fi

    $COMPOSE build
    build_rc=$?
    if [ $build_rc -ne 0 ]; then
        echo "!!! docker compose build failed (exit $build_rc) -- aborting, previous image/"
        echo "    container left running untouched"
        echo "=== LIVE deploy FAILED $(ts) ==="
        exit 1
    fi

    $COMPOSE up -d
    up_rc=$?
    if [ $up_rc -ne 0 ]; then
        echo "!!! docker compose up failed (exit $up_rc)"
        echo "=== LIVE deploy FAILED $(ts) ==="
        exit 1
    fi

    ok=0
    for i in $(seq 1 40); do
        if curl -sf -o /dev/null "$DASH_URL"; then
            ok=1
            break
        fi
        sleep 3
    done

    if [ "$ok" != "1" ]; then
        echo "!!! $DASH_URL not answering after ~120s"
        echo "=== LIVE deploy health check FAILED $(ts) -- proceeding to gateway-login anyway ==="
    else
        echo "LIVE deploy: $DASH_URL answering, dashboard container healthy"
    fi
    echo "$ok" > /tmp/_quant_live_deploy_health_ok
} >> "$LOG" 2>&1

health_ok=$(cat /tmp/_quant_live_deploy_health_ok 2>/dev/null || echo 0)
rm -f /tmp/_quant_live_deploy_health_ok

# Only actually attempts a real login if .env.live has real credentials -- during Stage 2
# (blank credentials) this will correctly abort fast via gateway-login-live.sh's own guard.
bash /home/cap/quant/scripts/gateway-login-live.sh "${1:-manual}" >> "$LOG" 2>&1
login_rc=$?
{
    if [ "$health_ok" = "1" ] && [ $login_rc -eq 0 ]; then
        echo "=== LIVE deploy OK $(ts) -- dashboard healthy, gateway login confirmed ==="
    elif [ "$health_ok" = "1" ]; then
        echo "=== LIVE deploy OK $(ts) -- dashboard healthy, gateway login NOT confirmed (see"
        echo "    gateway-login-live entries above) -- expected during Stage 2 (blank creds) =="
    else
        echo "=== LIVE deploy PARTIAL $(ts) -- dashboard health check did not pass within ~120s"
        echo "    (login_rc=$login_rc) -- check $DASH_URL and docker logs manually ==="
    fi
} >> "$LOG" 2>&1
exit 0
