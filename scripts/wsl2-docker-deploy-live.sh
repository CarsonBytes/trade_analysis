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
#   wsl -d Ubuntu -- bash /mnt/d/quant/scripts/wsl2-docker-deploy-live.sh [label] [--with-gateway]
#
# GATEWAY IS LEFT ALONE BY DEFAULT (2026-10-06, spec GATEWAY_PASSKEY P1). The live username is
# passkey-only now, and any recreate/restart of the gateway container throws away its session
# and demands a fresh passkey login at the PC. A dashboard deploy therefore only recreates the
# dashboard (`up -d --no-deps dashboard`). Pass --with-gateway to also apply gateway changes
# (Dockerfile.gateway / its compose block) -- do that only when you are at the PC to complete
# the passkey. A HELD gateway (scripts/gateway-hold.sh) is recreated but never started.
set -uo pipefail

LOG="/home/cap/quant-deploy.log"
DASH_URL="http://localhost:18081"
COMPOSE="docker compose -f docker-compose.live.yml -p quant-live"
GW_CONTAINER="quant-ibgateway-live-docker"
HOLD_FILE="/home/cap/.container-hold/$GW_CONTAINER"

WITH_GW=0
LABEL=""
for a in "$@"; do
    case "$a" in
        --with-gateway) WITH_GW=1 ;;
        *) [ -z "$LABEL" ] && LABEL="$a" ;;
    esac
done
LABEL="${LABEL:-manual}"
set -- "$LABEL"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

{
    echo "=== LIVE deploy started $(ts) (triggered by: ${1:-manual}, with-gateway=$WITH_GW) ==="

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
    # Since 2026-10-13 the Windows gateway IS the live gateway, so 4001 being up is the normal
    # state -- the warning below only matters for a --with-gateway (Docker gateway) deploy.
    if [ "$WITH_GW" = "1" ] && [ -n "$win_ip" ] && timeout 2 bash -c "cat < /dev/null > /dev/tcp/$win_ip/4001" 2>/dev/null; then
        echo "!!! WARNING: native Windows LIVE gateway (port 4001) appears to still be up"
        echo "    ($win_ip:4001 reachable) -- EXISTING_SESSION_DETECTED_ACTION should still"
        echo "    resolve this, but this WILL kick the native session the moment login"
        echo "    succeeds. Expected during Stage 3 cutover; unexpected any other time."
    fi

    # ADDED 2026-09-17 (cross-instance sharing): see paper's
    # wsl2-docker-deploy.sh -- same external-volume precondition.
    docker volume create quant_shared >/dev/null

    # 2026-10-13: the live gateway runs on WINDOWS now; the Docker one is parked behind the
    # docker-gateway profile, so --with-gateway must opt into that profile explicitly.
    if [ "$WITH_GW" = "1" ]; then
        COMPOSE="$COMPOSE --profile docker-gateway"
        $COMPOSE build
    else
        $COMPOSE build dashboard win-gateway-relay
    fi
    build_rc=$?
    if [ $build_rc -ne 0 ]; then
        echo "!!! docker compose build failed (exit $build_rc) -- aborting, previous image/"
        echo "    container left running untouched"
        echo "=== LIVE deploy FAILED $(ts) ==="
        exit 1
    fi

    if [ "$WITH_GW" = "1" ]; then
        if [ -f "$HOLD_FILE" ]; then
            # recreate with the new config but leave it stopped -- starting it would attempt a
            # login nobody is there to finish (see gateway-hold.sh)
            echo "LIVE deploy: gateway is HELD ($(cat "$HOLD_FILE")) -- recreating it STOPPED"
            $COMPOSE create --force-recreate ib-gateway-live
        else
            $COMPOSE up -d ib-gateway-live
        fi
    else
        echo "LIVE deploy: gateway untouched (no --with-gateway) -- its session survives this deploy"
    fi
    $COMPOSE up -d --no-deps win-gateway-relay dashboard
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
# Only when THIS deploy (re)started an unheld gateway (2026-10-06): an untouched gateway keeps
# its session, and typing credentials into it would at best be a wasted login attempt.
if [ "$WITH_GW" = "1" ] && [ ! -f "$HOLD_FILE" ]; then
    bash /home/cap/quant/scripts/gateway-login-live.sh "${1:-manual}" >> "$LOG" 2>&1
    login_rc=$?
else
    echo "[gateway-login-live $(ts)] skipped (gateway not redeployed, or held)" >> "$LOG"
    login_rc=2
fi
{
    if [ "$health_ok" = "1" ] && [ $login_rc -eq 0 ]; then
        echo "=== LIVE deploy OK $(ts) -- dashboard healthy, gateway login confirmed ==="
    elif [ "$health_ok" = "1" ] && [ $login_rc -eq 2 ]; then
        echo "=== LIVE deploy OK $(ts) -- dashboard healthy, gateway not part of this deploy ==="
    elif [ "$health_ok" = "1" ]; then
        echo "=== LIVE deploy OK $(ts) -- dashboard healthy, gateway login NOT confirmed (see"
        echo "    gateway-login-live entries above) -- expected during Stage 2 (blank creds) =="
    else
        echo "=== LIVE deploy PARTIAL $(ts) -- dashboard health check did not pass within ~120s"
        echo "    (login_rc=$login_rc) -- check $DASH_URL and docker logs manually ==="
    fi
} >> "$LOG" 2>&1
exit 0
