#!/bin/bash
# Codified gateway relogin cycle (spec F1/F2/F4, 2026-08-26) -- the deterministic single
# path for "this IBKR gateway is not logged in; bounce it, wait for login, tell the human".
# Used by:
#   - humans directly:  wsl -d Ubuntu -- bash /home/cap/quant/scripts/gateway-relogin.sh paper
#   - the pre-market cron (20:00 HKT Mon-Fri):  ... gateway-relogin.sh both scheduled
#   - gateway-login-watchdog.sh (stall detection), which calls this per-gateway
#
# What it does NOT do: click through IBKR's Second Factor Authentication. That push must
# be approved on the user's phone -- this script's job is to make sure a FRESH prompt is
# always generated (expired prompts are the #1 failure mode) and that success/failure is
# logged and pushed.
#
# Deterministic by design: fixed sequence, idempotent re-runs, every verdict appended to
# /home/cap/gateway-restart.log (the log whose absence proved this cron never ran).
set -uo pipefail

LOG=/home/cap/gateway-restart.log
POLL_SEC=${POLL_SEC:-180}
ts() { date '+%Y-%m-%d %H:%M:%S'; }

log() { echo "[$(ts)] $*" >> "$LOG"; }

container_for() {
    case "$1" in
        paper) echo quant-ibgateway-docker ;;
        live)  echo quant-ibgateway-live-docker ;;
    esac
}
port_hex_for() {
    case "$1" in
        paper) echo 0FA2 ;;   # native IB Gateway API port, paper
        live)  echo 0FA1 ;;   # native IB Gateway API port, live
    esac
}

# --- best-effort push notification: Telegram and/or ntfy --------------------
# Credentials come from the env if set, else from analyst/.env (rsynced to
# /home/cap/quant/analyst/.env by the deploy script). Never fails the script.
notify() {
    local msg="$1"
    local envfile=/home/cap/quant/analyst/.env
    local tg_token="${TELEGRAM_BOT_TOKEN:-}" tg_chat="${TELEGRAM_CHAT_ID:-}"
    local ntfy_url="${NTFY_URL:-}" ntfy_tok="${NTFY_TOKEN:-}"
    if [ -f "$envfile" ]; then
        [ -z "$tg_token" ] && tg_token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$envfile" | head -1 | cut -d= -f2-)
        [ -z "$tg_chat" ]  && tg_chat=$(grep -E '^TELEGRAM_CHAT_ID=' "$envfile" | head -1 | cut -d= -f2-)
        [ -z "$ntfy_url" ] && ntfy_url=$(grep -E '^NTFY_URL=' "$envfile" | head -1 | cut -d= -f2-)
        [ -z "$ntfy_tok" ] && ntfy_tok=$(grep -E '^NTFY_TOKEN=' "$envfile" | head -1 | cut -d= -f2-)
    fi
    if [ -n "$tg_token" ] && [ -n "$tg_chat" ]; then
        curl -sf -m 10 -X POST "https://api.telegram.org/bot${tg_token}/sendMessage" \
            -d chat_id="$tg_chat" -d text="⏳ [QTS] $msg" >/dev/null 2>&1 || true
    fi
    if [ -n "$ntfy_url" ]; then
        local auth=()
        [ -n "$ntfy_tok" ] && auth=(-H "Authorization: Bearer $ntfy_tok")
        curl -sf -m 10 -X POST "$ntfy_url" "${auth[@]}" \
            -H "Title: QTS gateway relogin" -H "Priority: high" \
            -d "$msg" >/dev/null 2>&1 || true
    fi
}

# --- port-open check: is the gateway's own API port LISTENing inside its netns?
# Checks both tcp and tcp6; socat's relay port always listens, so we must probe the
# TARGET hex port specifically -- a listening relay proves nothing about login state.
port_open() {
    local cname container_hex
    cname=$(container_for "$1"); container_hex=$(port_hex_for "$1")
    docker exec "$cname" sh -c \
        "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | grep -i \":${container_hex}\" | grep -qi ' 0A '"
}

# --- main -------------------------------------------------------------------
TARGET="${1:-both}"
REASON="${2:-manual}"
[ "$TARGET" = "both" ] && TARGETS="paper live" || TARGETS="$TARGET"

log "=== relogin started ($TARGET, reason: $REASON)"
for t in $TARGETS; do
    cname=$(container_for "$t")
    log "$t: restarting $cname"
    docker restart "$cname" >>"$LOG" 2>&1 || { log "$t: RESTART FAILED"; continue; }
done

for t in $TARGETS; do
    hex=$(port_hex_for "$t")
    waited=0
    while [ "$waited" -lt "$POLL_SEC" ]; do
        sleep 5; waited=$((waited + 5))
        if port_open "$t"; then break; fi
    done
    if port_open "$t"; then
        log "$t: LOGIN OK (API port ${hex} open after ~${waited}s)"
    else
        log "$t: STUCK after ${POLL_SEC}s -- API port ${hex} still closed (approve the IBKR phone push, or see gateway-login-watchdog.sh for auto-retry)"
        notify "IBKR ${t} gateway did NOT come back within ${POLL_SEC}s (reason: ${REASON}). Approve the second-factor prompt in the IBKR app now."
    fi
done
log "=== relogin finished ($TARGET)"
