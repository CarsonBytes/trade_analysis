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

    docker compose build dashboard
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

    if [ "$ok" = "1" ]; then
        echo "=== deploy OK $(ts) -- $DASH_URL answering ==="
        exit 0
    else
        echo "!!! $DASH_URL not answering after ~30s"
        echo "=== deploy FAILED health check $(ts) ==="
        exit 1
    fi
} >> "$LOG" 2>&1
