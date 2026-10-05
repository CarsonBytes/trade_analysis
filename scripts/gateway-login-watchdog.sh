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
# QUIET-hour outage signal (ADDED 2026-10-04). The weekend gate below correctly
# suppresses ACTION during quiet hours (a relogin would only push a 2FA prompt
# nobody can use), but it used to suppress ALL awareness too. Defaults chosen so
# a normal weekend -- where IBKR's weekly reset legitimately leaves the gateway
# logged out -- produces at most one informational message per quiet period, not
# a re-spam of the thing the gate exists to stop.
QUIET_ALERT_MIN=${QUIET_ALERT_MIN:-720}          #12h continuously down while quiet
QUIET_ALERT_REPEAT_MIN=${QUIET_ALERT_REPEAT_MIN:-1440}   # then at most once/day

ST=/home/cap/.gateway-watchdog
RELOGIN=/home/cap/quant/scripts/gateway-relogin.sh
HBLOG=/home/cap/cron-heartbeat.log
mkdir -p "$ST"
now=$(date +%s)
hourkey=$(date +%Y%m%d%H)

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> /home/cap/gateway-restart.log; }

# --- concurrency lock (ADDED 2026-10-04) ---------------------------------------
# This runs every 60s from cron while each relogin cycle it launches takes
# 35-90s (docker restart + IBC login + wait-for-port). Without a lock, the next
# cron fire lands mid-cycle and starts a SECOND cycle against the same container
# -- two competing `docker restart`s and two 2FA prompts. Confirmed by the
# observed cadence (286 cycles logged vs 332 "first seen CLOSED" markers).
# Non-blocking on purpose: if a run is still in flight we drop this minute and
# let the next fire handle it -- cron will be back shortly, so nothing is lost.
# Proof-of-life that cron itself is alive. Its ABSENCE is the alarm signal (the
# pre-market cron's missing log file is what hid its never-fired status until
# now). Cheap: one append per minute, and it must also be written by a run that
# exits early on the lock -- cron DID fire then, and a gap here would read as
# "cron died" to anything watching this file.
heartbeat() {
    echo "$now $(date '+%Y-%m-%d %H:%M:%S')" >> "$HBLOG"
    tail -200 "$HBLOG" > "${HBLOG}.tmp" 2>/dev/null && mv "${HBLOG}.tmp" "$HBLOG"
}

if command -v flock >/dev/null 2>&1; then
    exec 9>"$ST/run.lock"
    if ! flock -n 9; then
        heartbeat
        exit 0    # a previous run (and the relogin cycle it launched) still owns the lock
    fi
else
    log "WARNING: flock(1) unavailable -- running WITHOUT the concurrency lock"
fi

container_for() {
    case "$1" in
        paper) echo quant-ibgateway-docker ;;
        live)  echo quant-ibgateway-live-docker ;;
    esac
}
dashboard_container_for() {
    case "$1" in
        paper) echo quant-dashboard-docker ;;
        live)  echo quant-dashboard-live-docker ;;
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

# --- on-demand restart request (ADDED 2026-09-14) -----------------------------
# The dashboard's own "Restart" button used to shell out to powershell.exe + C:\IBC*
# directly -- pure dead code inside this Linux container (no powershell.exe, no C:\IBC,
# nothing reachable). Confirmed live: user report "restart function does not work, no
# 2fa" -- the button's app-restart half worked fine via Docker's own `restart:
# unless-stopped` policy, but the promised gateway kill+relaunch silently never
# happened (subprocess.Popen raised FileNotFoundError, caught by a bare except, logged
# nowhere the user would see). Now dashboard/app.py::_kill_and_relaunch_gateway() drops
# a flag file into its OWN container's /data volume instead; checked here via `docker
# exec` (matching this script's existing pattern, no new cross-container plumbing) on
# EVERY run so it fires within ~1 min of the click, deliberately BEFORE the market-quiet
# gate below -- an explicit user click is not the passive weekend-stall case that gate
# exists to suppress, and must always act.
restart_requested() {
    local dcname
    dcname=$(dashboard_container_for "$1")
    docker exec "$dcname" sh -c "test -f /data/restart_gateway_request_$1"
}
clear_restart_request() {
    local dcname
    dcname=$(dashboard_container_for "$1")
    docker exec "$dcname" sh -c "rm -f /data/restart_gateway_request_$1" 2>/dev/null || true
}
for t in paper live; do
    if restart_requested "$t"; then
        log "$t: on-demand gateway restart requested from the dashboard UI -- cycling relogin now"
        clear_restart_request "$t"
          # NOTIFICATION_SPEC: level=info -- the operator just clicked this
          # button, so the relogin is their own doing; buzzing them back is
          # pure echo. (The 2FA instruction still matters, which is why the
          # watchdog's own first-cycle page below carries it.)
          /home/cap/quant/scripts/gateway-push.sh info "manual-ui:$t" \
              "IBKR ${t} gateway restart requested from the dashboard -- relogin cycle started. APPROVE THE SECOND-FACTOR PROMPT IN THE IBKR APP (~2 min window)."
        bash "$RELOGIN" "$t" "manual-ui-restart" >/dev/null 2>&1
        rm -f "$ST/$t.since" "$ST/$t.attempts" "$ST/$t.quiet" \
              "$ST/$t.escalated" "$ST/$t.escalated.notified"
    fi
done

# --- weekend / holiday gate (ADDED 2026-09-05) --------------------------------
# This script had NO calendar gate at all while the scheduled relogin cron beside it is
# weekday-only (`0 20 * * 1-5`). IBKR's weekly server reset means the gateway cannot hold a
# login through much of the weekend, so the API port stays closed and this watchdog cycled a
# relogin -- each one a "APPROVE THE SECOND-FACTOR PROMPT" push to the phone -- three times
# an hour, around the clock. Measured 2026-08-29..31: 21 cycles Sat, 52 Sun, 31 Mon vs 4-11
# on a weekday. See dashboard/ops/gateway_window.py for why this asks the NYSE calendar
# rather than testing day-of-week (2026-09-07 is a Labor Day Monday).
# Fails ACTIVE: if the calendar says open (or cannot answer), behave exactly as before --
# a noisy phone beats an undetected outage. What it must NOT do is treat an unavailable
# CONTAINER as an ACTIVE answer (ADDED 2026-10-04): `docker exec` fails outright for the
# few seconds a dashboard recreate takes, and the old code could not tell that apart from
# a genuine "market is open". On a weekend, that single transient was enough to start a
# relogin cycle and push a 2FA prompt -- the exact spam this gate exists to stop.
#
# Why a sentinel instead of exit codes: `docker exec` on a missing container exits 1,
# which is the SAME code gateway_window.py returns for ACTIVE. Exit codes alone therefore
# cannot tell the two apart; only a token printed by a successful evaluation can.
#   QUIET/ACTIVE on stdout -> a real answer, cache it and use it
#   rc=2, no stdout        -> the calendar itself failed to evaluate -> ACTIVE
#   anything else          -> docker exec failed -> reuse the last real answer for
#                             QUIET_CACHE_MIN, then default ACTIVE
QUIET_CACHE="$ST/last_quiet"
QUIET_CACHE_MIN=${QUIET_CACHE_MIN:-30}
market_quiet() {
    local out rc last_rc last_ts
    out=$(docker exec -w /app -e PYTHONPATH=/app quant-dashboard-docker \
        /app/.venv/bin/python -m dashboard.ops.gateway_window 2>/dev/null)
    rc=$?
    case "$out" in
        QUIET)
            echo "0 $(date +%s)" > "$QUIET_CACHE"
            return 0
            ;;
        ACTIVE)
            echo "1 $(date +%s)" > "$QUIET_CACHE"
            return 1
            ;;
    esac
    [ "$rc" = "2" ] && return 1    # calendar evaluation failed -> ACTIVE (documented)
    # `read < file` errors are reported by the SHELL before any later 2>/dev/null takes
    # effect (redirections apply left to right), so test for the file explicitly rather
    # than trying to silence it.
    [ -f "$QUIET_CACHE" ] || return 1
    read -r last_rc last_ts < "$QUIET_CACHE" 2>/dev/null || return 1
    [ "$last_rc" = "0" ] || return 1
    [ $(( (now - last_ts) / 60 )) -le "$QUIET_CACHE_MIN" ] || return 1
    return 0
}
QUIET=0
if market_quiet; then QUIET=1; fi

for t in paper live; do
    if port_open "$t"; then
        # FIXED 2026-09-05: `.escalated` was never cleared on recovery, only the stall/attempt
        # files. Since the MANUAL-ACTION-NEEDED push is gated on that file NOT existing, the
        # single escalation on 2026-08-26 permanently disabled that alarm -- ten days later
        # the file was still there, so no exhausted-retries notification could ever fire
        # again on this machine. Recovery must reset the whole ladder, not part of it.
        rm -f "$ST/$t.since" "$ST/$t.attempts" "$ST/$t.quiet" \
              "$ST/$t.escalated" "$ST/$t.escalated.notified"
        continue
    fi

    # No session worth waking anyone for: clear the stall state exactly as the healthy
    # branch does, so the 5-minute clock starts FRESH when the window reopens and fires one
    # cycle then -- rather than a stale multi-day clock instantly burning all MAX_ATTEMPTS.
    if [ "$QUIET" -eq 1 ]; then
        # ADDED 2026-10-04: this branch used to also rm the two escalation files on EVERY
        # run. That had two consequences. (a) An alarm raised just before the gate engaged
        # was discarded the next minute, so the exhaustion notification could vanish without
        # ever being seen. (b) With the ladder state gone and no replacement signal, a
        # gateway that was down for a whole 12h quiet stretch was completely
        # indistinguishable from an idle weekend -- zero signal, to the very last minute.
        # So: reset only the ACTIVE-hours ladder (fresh clock on reopen, as designed), keep
        # the escalation latches, and emit ONE bounded outage notification instead.
        rm -f "$ST/$t.since" "$ST/$t.attempts"
        if [ ! -f "$ST/$t.quiet" ]; then
            touch "$ST/$t.quiet"
            echo "$now" > "$ST/$t.quiet_since"
            # a new quiet period is a new story: forget the previous period's escalation
            # and alert state so they cannot suppress this one
            rm -f "$ST/$t.escalated" "$ST/$t.escalated.notified" "$ST/$t.quiet.alerted"
            log "$t: API port closed, but no US session within reach (weekend/holiday) -- " \
                "watchdog quiet, no relogin and no phone push until the window reopens"
        fi
        qsince=$(cat "$ST/$t.quiet_since" 2>/dev/null || echo "$now")
        qage_min=$(( (now - qsince) / 60 ))
        if [ "$qage_min" -ge "$QUIET_ALERT_MIN" ]; then
            last_alert=$(cat "$ST/$t.quiet.alerted" 2>/dev/null || echo 0)
            if [ $(( (now - last_alert) / 60 )) -ge "$QUIET_ALERT_REPEAT_MIN" ]; then
                echo "$now" > "$ST/$t.quiet.alerted"
                log "$t: API port STILL closed after ${qage_min}min of quiet hours -- " \
                    "sending the bounded outage notification (relogin remains suppressed)"
                  # NOTIFICATION_SPEC: level=error -- expected during IBKR's
                  # weekly reset, and the relogin stays suppressed anyway, so
                  # there is nothing to act on. Rolls into the digest.
                  /home/cap/quant/scripts/gateway-push.sh error "quiet:$t" \
                      "IBKR ${t} gateway down for $((qage_min / 60))h while markets are shut. Expected during IBKR's weekly reset -- but if you expected it logged in, investigate."
            fi
        fi
        continue
    fi
    rm -f "$ST/$t.quiet"

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
            # NOTIFICATION_SPEC: level=critical -- auto-relogin has exhausted
            # its attempts and a human has to open the IBKR app. This is the
            # one gateway message that meets the decision rule.
            /home/cap/quant/scripts/gateway-push.sh critical "escalate:$t" \
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
        # NOTIFICATION_SPEC: page on the FIRST cycle of an incident only.
        # Every attempt carried the same "approve the 2FA prompt" instruction,
        # so pushing all of them turned 299 cycles into 299 phone buzzes with
        # no extra action available to the operator. A later hour of the same
        # outage re-arms `attempts` and pages again, and the MAX_ATTEMPTS
        # escalation above still pages once when auto-relogin gives up.
        #
        # FIXED 2026-10-05: `attempts` is keyed on $hourkey, so "first cycle of an
        # incident" was really "first cycle of each HOUR". Combined with the `rm -f
        # .since` below resetting the stall clock, a gateway whose port stayed closed
        # across the whole pre-open window re-armed hourly and paged once per hour --
        # measured 3 cycles/hour for 4 hours on 2026-10-05, 9 pages for one outage.
        # The incident key below is anchored to when the port was FIRST seen closed
        # (`since`, captured before the clock is reset), so one continuous outage pages
        # once no matter how many hours it spans. A genuine recovery + new outage starts
        # a fresh key because `since` is re-created in the port_open() branch above.
        incident_key=$(printf '%s' "$since")
        if [ "$n" -eq 1 ]; then
            /home/cap/quant/scripts/gateway-push.sh critical "cycle:${t}:${incident_key}" \
                "IBKR ${t} gateway not logged in -- relogin cycle ${n}/${MAX_ATTEMPTS} started. APPROVE THE SECOND-FACTOR PROMPT IN THE IBKR APP (~2 min window)."
        fi
        bash "$RELOGIN" "$t" "watchdog-cycle-${n}" >/dev/null 2>&1
    fi
done

# --- heartbeat: proof-of-life that cron itself is alive ----------------------
# (Written via heartbeat(), which also runs on the lock-skip path above.)
heartbeat
