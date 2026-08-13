#!/bin/bash
# Deterministic, idempotent redeploy of the PARALLEL WSL2/Docker quant deployment (paper-only,
# port 18080 -- see HANDOFF.md's 2026-08-12 entry for the full deployment writeup). Runs
# alongside the native Windows Task Scheduler deployment; never touches it.
#
# "Deterministic" here means: (1) every build input is digest-pinned (Dockerfile, docker-
# compose.yml) so the SAME quant commit always produces the SAME deployed image, not whatever
# an upstream floating tag happens to resolve to that day; (2) this script always runs the same
# fixed sequence, no conditional branches based on prior state -- rsync --delete + docker
# compose build + up -d converge to the correct result regardless of what was running before,
# so re-running this after a failed attempt is always safe, not something that compounds a bad
# state.
#
# Invoked by .git/hooks/pre-push (machine-local, not tracked -- see that file for why) in the
# BACKGROUND, so `git push` itself is never slowed down by a rebuild. Safe to run manually too:
#   wsl -d Ubuntu -- bash /mnt/d/quant/scripts/wsl2-docker-deploy.sh
set -uo pipefail   # NOT -e: a failed step must still fall through to the log-and-exit-1 below,
                   # not abort mid-script leaving no record of what happened

LOG="/home/cap/quant-deploy.log"
DASH_URL="http://localhost:18080"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

{
    echo "=== deploy started $(ts) (triggered by: ${1:-manual}) ==="

    rsync -a --delete \
        --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
        --exclude='*.pyc' --exclude='*.db' --exclude='logs' --exclude='.env' \
        /mnt/d/quant/ /home/cap/quant/
    rsync_rc=$?
    if [ $rsync_rc -ne 0 ]; then
        echo "!!! rsync failed (exit $rsync_rc) -- aborting, previous deployment left running"
        echo "=== deploy FAILED $(ts) ==="
        exit 1
    fi

    cd /home/cap/quant || { echo "!!! /home/cap/quant missing after rsync"; exit 1; }

    # Informational precondition, not a hard abort -- EXISTING_SESSION_DETECTED_ACTION=primary
    # (docker-compose.yml) makes IBC win a session conflict automatically now, confirmed live
    # 2026-08-13. But a native paper Gateway left running would still mean the two repeatedly
    # fight over the same IBKR session (confirmed live: a real tug-of-war loop before the
    # native DashboardApp task was disabled) -- wasteful even when each individual round
    # resolves cleanly. `localhost` from WSL2 does NOT reach the Windows host here (confirmed
    # live -- even a known-open port failed) -- must use the actual default-route gateway IP,
    # resolved fresh each run since it isn't guaranteed stable across WSL2 restarts.
    win_ip=$(ip route | awk '/^default/ {print $3; exit}')
    if [ -n "$win_ip" ] && timeout 2 bash -c "cat < /dev/null > /dev/tcp/$win_ip/4002" 2>/dev/null; then
        echo "!!! WARNING: native Windows paper Gateway (port 4002) appears to still be up"
        echo "    ($win_ip:4002 reachable) -- EXISTING_SESSION_DETECTED_ACTION should still"
        echo "    resolve this, but expect an extra login cycle. Disable the native"
        echo "    DashboardApp scheduled task for a clean run."
    fi

    docker compose build
    build_rc=$?
    if [ $build_rc -ne 0 ]; then
        echo "!!! docker compose build failed (exit $build_rc) -- aborting, previous image/"
        echo "    container left running untouched"
        echo "=== deploy FAILED $(ts) ==="
        exit 1
    fi

    docker compose up -d
    up_rc=$?
    if [ $up_rc -ne 0 ]; then
        echo "!!! docker compose up failed (exit $up_rc)"
        echo "=== deploy FAILED $(ts) ==="
        exit 1
    fi

    # Bounded health check -- 10 tries, 3s apart (~30s), same cadence used to verify this
    # deployment manually. A container that's "Up" but not yet answering (still starting) is
    # not a failure until this window is exhausted.
    ok=0
    for i in $(seq 1 10); do
        if curl -sf -o /dev/null "$DASH_URL"; then
            ok=1
            break
        fi
        sleep 3
    done

    if [ "$ok" != "1" ]; then
        echo "!!! $DASH_URL not answering after ~30s"
        echo "=== deploy FAILED health check $(ts) ==="
        exit 1
    fi
    echo "deploy: $DASH_URL answering, dashboard container healthy"
} >> "$LOG" 2>&1

# gateway-login.sh has its own log() calls appending to the same $LOG -- run it OUTSIDE the
# redirect block above so its output isn't double-wrapped, but still sequenced after a
# confirmed-healthy dashboard container. A login failure here does NOT fail this whole script
# (the dashboard itself deployed fine and will retry its own connection on the next cheap-
# refresh cycle regardless) -- logged clearly either way.
bash /home/cap/quant/scripts/gateway-login.sh "${1:-manual}" >> "$LOG" 2>&1
login_rc=$?
{
    if [ $login_rc -eq 0 ]; then
        echo "=== deploy OK $(ts) -- dashboard healthy, gateway login confirmed ==="
    else
        echo "=== deploy OK $(ts) -- dashboard healthy, gateway login NOT confirmed (see"
        echo "    gateway-login entries above) -- will retry on its own connection cycle ==="
    fi
} >> "$LOG" 2>&1
exit 0
