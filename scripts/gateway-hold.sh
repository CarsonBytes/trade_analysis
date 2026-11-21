#!/bin/bash
# Gateway HOLD switch (ADDED 2026-10-06, spec GATEWAY_PASSKEY P0).
#
# Why this exists: on 2026-10-03 IBKR switched the LIVE username's only second factor to a
# PASSKEY (SFC Hong Kong mandate). Verified in the gateway's own launcher.log: every
# NS_AUTH_START since then advertises exactly `token(s)=[token=5, tokenSubtype=4[Passkey]]`.
# A headless Docker gateway cannot complete a passkey on its own (no phone push, no
# Bluetooth), so every automatic relogin is a guaranteed failure -- 105 of them over
# 10-04/10-05, 89 answered "Too many failed login attempts" by IBKR. Automation retrying a
# login nobody can complete makes the lockout WORSE.
#
# A hold means: the gateway container is STOPPED and every automatic actor leaves it alone --
#   gateway-login-watchdog.sh  no stall cycles (a dashboard "Restart" click still acts)
#   gateway-relogin.sh         skips held targets unless the reason starts with "manual"
#   infra-watchdog.sh          does not restart a held container that is not running
#   wsl2-docker-deploy-live.sh never starts a held gateway
# The hold is ONE file per container under $HOLD_DIR, shared with infra-watchdog.sh
# (llm-usage-dashboard repo), so the two never disagree about what "held" means.
#
# Usage (from Windows):  wsl -d Ubuntu -- bash /home/cap/quant/scripts/gateway-hold.sh <cmd> live
#   hold    <paper|live> [reason]   stop the gateway and keep every automatic actor off it
#   release <paper|live>            clear the hold, start the gateway, and wait for the
#                                   login -- run this AT THE GATEWAY PC, ready to complete
#                                   the passkey (P2), because IBC logs in immediately
#   status                          show holds and gateway state
set -uo pipefail

HOLD_DIR=/home/cap/.container-hold
LOG=/home/cap/gateway-restart.log
ST=/home/cap/.gateway-watchdog
WAIT_SEC=${WAIT_SEC:-300}
mkdir -p "$HOLD_DIR" "$ST"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

container_for() {
    case "$1" in
        paper) echo quant-ibgateway-docker ;;
        live)  echo quant-ibgateway-live-docker ;;
        *)     echo "" ;;
    esac
}
port_hex_for() {
    case "$1" in
        paper) echo 0FA2 ;;
        live)  echo 0FA1 ;;
    esac
}
port_open() {
    docker exec "$(container_for "$1")" sh -c \
        "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | grep -i \":$(port_hex_for "$1")\" | grep -qi ' 0A '" 2>/dev/null
}

cmd="${1:-status}"
t="${2:-}"

case "$cmd" in
    hold)
        c=$(container_for "$t"); [ -n "$c" ] || { echo "usage: $0 hold <paper|live> [reason]"; exit 2; }
        reason="${3:-manual hold}"
        echo "$(date '+%Y-%m-%d %H:%M:%S') $reason" > "$HOLD_DIR/$c"
        log "$t: HOLD set ($reason) -- stopping $c; automatic relogin/restart suppressed until released"
        docker stop "$c" >/dev/null 2>&1 || log "$t: docker stop $c failed (already stopped or missing)"
        # the watchdog's stall ladder is meaningless while held -- start fresh on release
        rm -f "$ST/$t.since" "$ST/$t.attempts" "$ST/$t.escalated" "$ST/$t.escalated.notified"
        ;;
    release)
        c=$(container_for "$t"); [ -n "$c" ] || { echo "usage: $0 release <paper|live>"; exit 2; }
        rm -f "$HOLD_DIR/$c"
        log "$t: HOLD released by a human -- starting $c; complete the second factor (passkey) NOW, ${WAIT_SEC}s window"
        docker start "$c" >/dev/null 2>&1 || docker restart "$c" >/dev/null 2>&1
        waited=0
        while [ "$waited" -lt "$WAIT_SEC" ]; do
            sleep 5; waited=$((waited + 5))
            if port_open "$t"; then
                log "$t: LOGIN OK after ~${waited}s (released hold)"
                exit 0
            fi
            [ $((waited % 30)) -eq 0 ] && echo "  ...${waited}s, still waiting for the second factor"
        done
        log "$t: release did NOT log in within ${WAIT_SEC}s -- the watchdog will re-hold it if the account is passkey-only"
        exit 1
        ;;
    status)
        for t in paper live; do
            c=$(container_for "$t")
            st=$(docker inspect --format '{{.State.Status}}' "$c" 2>/dev/null | tr -d '\r\n')
            if port_open "$t"; then p=LOGGED-IN; else p=not-logged-in; fi
            if [ -f "$HOLD_DIR/$c" ]; then h="HELD since $(cat "$HOLD_DIR/$c")"; else h="not held"; fi
            echo "$t: container=$st api=$p $h"
        done
        ;;
    *)
        echo "usage: $0 {hold <t> [reason]|release <t>|status}"; exit 2 ;;
esac
