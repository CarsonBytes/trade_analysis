#!/bin/bash
# Gateway LOGIN watchdog -- spec F1 (2026-08-26). Runs every minute via cap's crontab,
# alongside docker-watchdog.sh (which only watches the DASHBOARD containers' health and
# is deliberately blind to gateway login state).
#
# The failure mode this closes (found live 2026-08-26): after any container restart, IBKR
# login can park at a modal ("Second Factor Authentication", "Login Messages") forever.
# The phone push expires in ~2 minutes; TWOFA_TIMEOUT_ACTION=restart proved unreliable;
# nothing re-prompts; the dashboards keep showing CACHED account data looking healthy.
# This watchdog makes a fresh prompt appear automatically, with bounded retries, and
# escalates to a pushed notification when automation is exhausted.
#
# Detection: the gateway's OWN API port (paper 4002 / live 4001) LISTENing inside its
# netns. The socat relay port always listens regardless of login state, so relay ports
# prove nothing -- this checks the target port specifically. State is kept in tiny files
# under /home/cap/.gateway-watchdog/ so the script stays stateless-safe across cron fires.
#
# Escalation ladder per gateway:
#   closed >= STALL_MIN        -> restart #1 + "approve the push NOW" notification
#   still closed every RETRY_GAP_MIN -> restart again (up to MAX_ATTEMPTS within an hour)
#   MAX_ATTEMPTS hit           -> MANUAL-ACTION-NEEDED notification, stop retrying this hour
set -uo pipefail

STALL_MIN=${STALL_MIN:-5}
RETRY_GAP_MIN=${RETRY_GAP_MIN:-3}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}

ST=/home/cap/.gateway-watchdog
RELOGIN=/home/cap/quant/scripts/gateway-relogin.sh
HBLOG=/home/cap/cron-heartbeat.log
mkdir -p "$ST"
now=$(date +%s)
hourkey=$(date +%Y%m%d%H)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> /home/cap/gateway-restart.log; }

container_for() {
    case "$1" in
        paper) echo quant-ibgateway-docker ;;
        live)  echo quant-ibgateway-live-docker ;;
    esac
}
port_hex_for() {
    case "$1" in
        paper) echo 0FA2 ;;
        live)  echo 0FA1 ;;
    esac
}

port_open() {
    local cname container_hex
    cname=$(container_for "$1"); container_hex=$(port_hex_for "$1")
    docker exec "$cname" sh -c \
        "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | grep -i \":${container_hex}\" | grep -qi ' 0A '"
}

for t in paper live; do
    if port_open "$t"; then
        rm -f "$ST/$t.since" "$ST/$t.attempts"      # healthy: clear stall tracking
        continue
    fi

    # --- track how long the port has been closed (consecutive-minute state) ------
    if [ ! -f "$ST/$t.since" ]; then
        echo "$now" > "$ST/$t.since"
        log "$t: API port first seen CLOSED -- stall clock started ($STALL_MIN min to first auto-cycle)"
        continue
    fi
    since=$(cat "$ST/$t.since" 2>/dev/null || echo "$now")
    age_min=$(( (now - since) / 60 ))

    attempts=0
    if [ -f "$ST/$t.attempts" ]; then
        read -r akey acount < "$ST/$t.attempts" 2>/dev/null || { akey=""; acount=0; }
        [ "$akey" = "$hourkey" ] && attempts=$acount || attempts=0
    fi

    last=0
    [ -f "$ST/$t.lastattempt" ] && last=$(cat "$ST/$t.lastattempt" 2>/dev/null || echo 0)
    gap_min=$(( (now - last) / 60 ))

    if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
        if [ ! -f "$ST/$t.escalated" ]; then
            echo "$now" > "$ST/$t.escalated"
            log "$t: $attempts auto-relogin cycles failed within this hour -- ESCALATING to manual action"
            /home/cap/quant/scripts/gateway-push.sh \
                "IBKR ${t} gateway: ${MAX_ATTEMPTS} automatic relogin cycles FAILED. Manual action needed: check IBKR app / gateway logs."
            touch "$ST/$t.escalated.notified"
        fi
        continue
    fi

    # fire a cycle on the FIRST threshold crossing, then no more often than RETRY_GAP_MIN
    if [ "$age_min" -ge "$STALL_MIN" ] && [ "$gap_min" -ge "$RETRY_GAP_MIN" ]; then
        n=$((attempts + 1))
        log "$t: port closed ${age_min}min (attempt ${n}/${MAX_ATTEMPTS}) -- cycling relogin"
        echo "$hourkey $n" > "$ST/$t.attempts"
        echo "$now" > "$ST/$t.lastattempt"
        rm -f "$ST/$t.since"                        # reset the stall clock for the new attempt
        /home/cap/quant/scripts/gateway-push.sh \
            "IBKR ${t} gateway not logged in -- relogin cycle ${n}/${MAX_ATTEMPTS} started. APPROVE THE SECOND-FACTOR PROMPT IN THE IBKR APP (~2 min window)."
        bash "$RELOGIN" "$t" "watchdog-cycle-${n}" >/dev/null 2>&1
    fi
done

# --- heartbeat: proof-of-life that cron itself is alive ----------------------
# Its ABSENCE is the alarm signal (the pre-market cron's missing log file is what hid
# its never-fired status until now). Cheap: one append per minute.
echo "$now $(date '+%Y-%m-%d %H:%M:%S')" >> "$HBLOG"
tail -200 "$HBLOG" > "${HBLOG}.tmp" 2>/dev/null && mv "${HBLOG}.tmp" "$HBLOG"
