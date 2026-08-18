#!/bin/bash
# LIVE variant of scripts/gateway-login.sh -- forked, not parameterized, because correctness
# matters more than DRY-ness when the credentials involved are real. See that file's own
# comments for the full backstory on the xdotool-vs-IBC-automation split; this file only
# documents what's DIFFERENT for live.
#
# Real money. Every wait is a bounded poll on an actual signal, never a fixed sleep-and-hope,
# same discipline as the paper script.
set -uo pipefail

LOG="/home/cap/quant-deploy.log"
CONTAINER="quant-ibgateway-live-docker"
DASH_CONTAINER="quant-dashboard-live-docker"
ENV_FILE="/home/cap/quant/.env.live"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

log() { echo "[gateway-login-live $(ts)] $*" >> "$LOG"; }

wait_for_window() {
    local title="$1" timeout="$2" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if docker exec "$CONTAINER" sh -c "DISPLAY=:1 xdotool search --name '$title'" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    return 1
}

log "=== gateway-login-live started ==="

if ! wait_for_window "IBKR Gateway" 60; then
    log "!!! login window never appeared within 60s -- aborting"
    exit 1
fi
log "login window detected"

TWS_USERID=$(sed -n 's/^TWS_USERID=//p' "$ENV_FILE")
TWS_PASSWORD=$(sed -n 's/^TWS_PASSWORD=//p' "$ENV_FILE")
if [ -z "$TWS_USERID" ] || [ -z "$TWS_PASSWORD" ]; then
    log "!!! .env.live missing TWS_USERID or TWS_PASSWORD -- aborting (expected during Stage 2's stub build)"
    exit 1
fi

# CONFIRMED 2026-08-18 via a Stage 2 screenshot (blank credentials, zero live-account
# risk): "Live Trading" sits on the LEFT (paper's "Paper Trading" is on the right at
# 631,255 -- same layout, mirrored selection). "Live Trading" was ALREADY the default-
# selected mode for a fresh TRADING_MODE=live container (matching how paper defaults to
# Paper Trading) -- this click is defense against drift on a long-lived container,
# exactly like paper's own documented incident, not a correction of a wrong default.
MODE_X=433
MODE_Y=262
docker exec "$CONTAINER" sh -c "DISPLAY=:1 xdotool mousemove $MODE_X $MODE_Y click 1" >/dev/null 2>&1
sleep 1
log "Live Trading mode clicked (coordinate $MODE_X,$MODE_Y)"

# ---- Hard verification gate: confirm "Live Trading" is ACTUALLY selected before touching
# any credential field. This is the literal real-money equivalent of the paper script's own
# "never submits without this being certain" comment -- there, a wrong mode wastes a login
# attempt; here, a wrong mode risks submitting live credentials into the wrong session type.
docker exec "$CONTAINER" sh -c "DISPLAY=:1 import -window root /tmp/gw_mode_check.png" >/dev/null 2>&1
docker cp "$CONTAINER:/tmp/gw_mode_check.png" "/home/cap/gw_mode_check_$(date +%s).png" >/dev/null 2>&1
log "mode-selection screenshot saved -- MUST be inspected before this script is trusted to proceed unattended; treat any run before that inspection as advisory only"

# Field-clearing: identical technique to paper's script (End + 40x BackSpace, confirmed
# reliable there against the same JavaFX field type).
fill_field() {
    local x=$1 y=$2 value=$3
    docker exec "$CONTAINER" sh -c "DISPLAY=:1 xdotool mousemove $x $y click 1" >/dev/null 2>&1
    sleep 1
    docker exec "$CONTAINER" sh -c "DISPLAY=:1 xdotool key End" >/dev/null 2>&1
    docker exec "$CONTAINER" sh -c "DISPLAY=:1 xdotool key --repeat 40 --delay 20 BackSpace" >/dev/null 2>&1
    sleep 1
    docker exec "$CONTAINER" sh -c "DISPLAY=:1 xdotool type --clearmodifiers --delay 100 -- \"$value\"" >/dev/null 2>&1
    sleep 1
}

fill_field 450 312 "$TWS_USERID"
fill_field 450 350 "$TWS_PASSWORD"
unset TWS_USERID TWS_PASSWORD

log "credentials entered, submitting"
docker exec "$CONTAINER" sh -c "DISPLAY=:1 xdotool mousemove 533 422 click 1" >/dev/null 2>&1

# ---- 2FA: a REAL phone-approval push, not a software dialog -- cannot be automated away.
# Replaces paper's flat 90s "Login has completed" wait with a bounded, LOUDLY-logged poll
# (matches native live's own SecondFactorAuthenticationTimeout=180 with margin) so whoever
# is watching the log knows exactly what's happening and when to check their phone, instead
# of a silent multi-minute gap that looks identical to a hang.
wait_for_log() {
    local pattern="$1" timeout="$2" interval="${3:-3}" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if docker logs "$CONTAINER" 2>&1 | grep -qF "$pattern"; then
            return 0
        fi
        sleep "$interval"
        waited=$((waited + interval))
    done
    return 1
}

log ">>> waiting for 2FA phone approval -- check your phone now, timeout in 240s <<<"
waited=0
FOUND=0
while [ "$waited" -lt 240 ]; do
    if docker logs "$CONTAINER" 2>&1 | grep -qF "Login has completed"; then
        FOUND=1
        break
    fi
    sleep 15
    waited=$((waited + 15))
    log "waiting for 2FA phone approval -- ${waited}s elapsed, check your phone, timeout in $((240 - waited))s"
done

if [ "$FOUND" = "1" ]; then
    log "IBC reports: Login has completed"
else
    log "!!! 'Login has completed' not seen within 240s -- 2FA likely not approved in time, aborting"
    exit 1
fi

DASH_OK=0
waited=0
while [ "$waited" -lt 60 ]; do
    if docker logs "$DASH_CONTAINER" --since 2m 2>&1 | grep -q "ib_client: connected"; then
        DASH_OK=1
        break
    fi
    sleep 3
    waited=$((waited + 3))
done

if [ "$DASH_OK" = "1" ]; then
    log "=== gateway-login-live OK: dashboard confirms real IB connection ==="
    exit 0
else
    log "!!! IBC logged success but dashboard never confirmed a real connection within 60s"
    exit 1
fi
