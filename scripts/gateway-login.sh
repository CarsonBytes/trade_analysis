#!/bin/bash
# Automates the ONE step in the paper Gateway login flow that IBC's own automation is
# confirmed broken for on this specific image (ghcr.io/gnzsnz/ib-gateway, build 10.45.1j):
# filling and submitting the initial username/password form. That form is rendered by a
# JavaFX-based UI that doesn't receive IBC's AWT-Robot-driven keystrokes under Xvfb (confirmed
# live 2026-08-12: IBC's own log claimed "Setting user name"/"Setting password" succeeded, but
# a screenshot showed the fields still empty -- identical outcome whether credentials were
# blank or real, ruling out a credentials/session-conflict explanation). xdotool's direct X11
# XTest keystroke injection works where IBC's Robot-based approach doesn't.
#
# Everything AFTER that first form is left to IBC's own (already-proven-reliable) automation:
# "Re-login is required" was handled automatically with zero help from this script, and
# EXISTING_SESSION_DETECTED_ACTION=primary (docker-compose.yml) should make it handle
# "Existing session detected" automatically too, the same way. This script does not try to
# reimplement dialog-handling IBC already does correctly.
#
# State-based verification throughout -- no screenshots, no visual inspection. Every wait is
# a bounded poll on an actual signal (window existence, log content, port state), never a
# fixed sleep-and-hope.
set -uo pipefail

LOG="/home/cap/quant-deploy.log"
CONTAINER="quant-ibgateway-docker"
DASH_CONTAINER="quant-dashboard-docker"
ENV_FILE="/home/cap/quant/.env"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

log() { echo "[gateway-login $(ts)] $*" >> "$LOG"; }

# ---- precondition: native paper Gateway must be down, not raced against -------------------
# Checked from the WSL2 side (can't reach into Windows directly) via the published debug port
# -- if IBC's own EXISTING_SESSION_DETECTED_ACTION=primary is relied on to win a race against
# a still-running native session, every native watchdog cycle (auto-relaunch) would re-trigger
# another round of dialogs -- confirmed exactly this failure mode live 2026-08-12 before the
# native DashboardApp task was disabled. Fail loudly instead of silently racing.
#
# NOTE: this can only detect the DOCKER-side symptom (repeated Existing-session dialogs in the
# log, checked after the attempt below) -- it cannot directly probe the native Windows process
# from inside WSL2. Documented precondition: DashboardApp scheduled task must be Disabled
# before running this script.

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

log "=== gateway-login started ==="

if ! wait_for_window "IBKR Gateway" 60; then
    log "!!! login window never appeared within 60s -- aborting"
    exit 1
fi
log "login window detected"

# Read credentials with sed (pure text extraction, no shell interpretation of the value) --
# `source`-ing .env directly was tried first and silently truncated a password containing a
# shell-special character. Never echoed; only ever held in-memory in this process.
TWS_USERID=$(sed -n 's/^TWS_USERID=//p' "$ENV_FILE")
TWS_PASSWORD=$(sed -n 's/^TWS_PASSWORD=//p' "$ENV_FILE")
if [ -z "$TWS_USERID" ] || [ -z "$TWS_PASSWORD" ]; then
    log "!!! .env missing TWS_USERID or TWS_PASSWORD -- aborting"
    exit 1
fi

# Force Paper Trading mode explicitly -- a fresh container correctly defaults to it
# (confirmed 2026-08-13), but a container that's been up a while with prior automated
# attempts can drift to Live Trading selected (confirmed live: a long-running container
# showed Live Trading highlighted after several earlier failed attempts). Never submit
# without this being certain -- this is real-account credential entry, not cosmetic.
docker exec "$CONTAINER" sh -c "DISPLAY=:1 xdotool mousemove 631 255 click 1" >/dev/null 2>&1
sleep 1
log "Paper Trading mode selected"

# Fixed coordinates are safe here because Xvfb's resolution is pinned (1024x768x16, set by
# the image's own startup, confirmed via `ps aux` inside the container) -- not a brittle
# guess, a property of a controlled environment.
#
# Clearing method: ctrl+a followed by a SINGLE BackSpace was confirmed unreliable in this
# specific JavaFX field (live 2026-08-13: a container left running through several earlier
# automated attempts ended up with garbled/concatenated text in the username field, meaning
# ctrl+a's select-all didn't reliably clear prior content in this component). Fixed to send
# End (move cursor to the end of any existing content) then 40 individual BackSpace presses
# -- character-by-character, not selection-dependent, so it can't leave a partial remainder
# regardless of this field's actual selection semantics.
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

# ---- state-based success verification -------------------------------------------------
# Two independent signals, not one: IBC's own documented success marker in its log, AND the
# dashboard container's own successful API connection (the real functional proof -- a log
# line and a working connection are different claims, confirmed distinct earlier this session
# when the log falsely claimed field values were set while the GUI showed them empty).
wait_for_log() {
    local pattern="$1" timeout="$2" waited=0
    while [ "$waited" -lt "$timeout" ]; do
        if docker logs "$CONTAINER" 2>&1 | grep -qF "$pattern"; then
            return 0
        fi
        sleep 3
        waited=$((waited + 3))
    done
    return 1
}

if wait_for_log "Login has completed" 90; then
    log "IBC reports: Login has completed"
else
    log "!!! 'Login has completed' not seen within 90s"
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
    log "=== gateway-login OK: dashboard confirms real IB connection ==="
    exit 0
else
    log "!!! IBC logged success but dashboard never confirmed a real connection within 60s"
    exit 1
fi
