#!/bin/bash
# Quant weekend sleep (QUANT_WEEKEND_SLEEP_SPEC, 2026-11-21): stop the quant containers
# between the Friday close and the weekly IBKR login to give ~1.2 GB back to Windows.
#
#   weekend-docker.sh tick [--dry-run]   cron, every 5 min: make reality match the window
#   weekend-docker.sh sleep              manual: sleep now, until the next wake time
#   weekend-docker.sh wake               manual: wake now, stay awake for the rest of this weekend
#   weekend-docker.sh status
#
# Window (HKT, from C:\IBC-Live\weekly-touch.json, the same file as the weekend gateway hold):
#   sleepStartDay/sleepStartTime (default Saturday 06:30) -> wakeDay/wakeTime (default Sunday 20:00)
#
# LEVEL-triggered: every tick recomputes the desired state, so a missed minute (box asleep,
# the 20:45 daily `wsl --shutdown`, a reboot) self-corrects on the next tick.
#
# Holds: before stopping, each container gets /home/cap/.container-hold/<name> with a
# `weekend-sleep ...` line. infra-watchdog.sh and watchdog.ps1 skip held containers. Wake
# removes ONLY weekend-sleep holds; a hold with any other reason (e.g. the parked Docker live
# gateway) is never touched, and such a container is not stopped or started here.
#
# Alerts go through gateway-push.sh: sleep/wake ok = `info` (digest only -- `critical` is
# Telegram and capped at 10/day, which a routine notice must not spend); a container that
# does not come back on wake = `critical`.
#
# DISPLAY MODE (QUANT_WEEKEND_DISPLAY_SPEC, 2026-11-28): with mode=display (env WEEKEND_MODE
# or the file $STATE_DIR/mode), the dashboards are NOT stopped. Only the paper gateway sleeps;
# each dashboard instead gets /data/api-off (a self-expiring JSON flag) and stays up for
# display with the broker API off. If a dashboard does not confirm api_mode=off on /status
# within API_VERIFY_TIMEOUT, it falls back to being held + stopped (the old behaviour) for the
# rest of that weekend, with a critical push. Default mode is `sleep` (unchanged behaviour).
#
# Everything is overridable by env for scripts/weekend_docker_test.sh (NOW_EPOCH, DOCKER, ...).
set -uo pipefail

CONFIG="${CONFIG:-/mnt/c/IBC-Live/weekly-touch.json}"
HOLD_DIR="${HOLD_DIR:-/home/cap/.container-hold}"
STATE_DIR="${STATE_DIR:-/home/cap/.weekend-sleep}"
LOG="${LOG:-/home/cap/weekend-docker.log}"
CONTAINERS="${CONTAINERS:-quant-dashboard-live-docker quant-dashboard-docker quant-ibgateway-docker}"
PUSH="${PUSH:-/home/cap/quant/scripts/gateway-push.sh}"
DOCKER="${DOCKER:-docker}"
HEALTH_PORTS="${HEALTH_PORTS-18080 18081}"
WAKE_TIMEOUT="${WAKE_TIMEOUT:-180}"
STOP_TIMEOUT="${STOP_TIMEOUT:-60}"
DROP_CACHE="${DROP_CACHE:-1}"
# No passwordless sudo in this distro; WSL interop reaches root via wsl.exe (works from cron).
DROP_CACHE_CMD="${DROP_CACHE_CMD:-/mnt/c/Windows/System32/wsl.exe -u root -- sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'}"
DRY_RUN="${DRY_RUN:-0}"
WEEKEND_TAG="weekend-sleep"
CURL="${CURL:-curl}"
MODE_FILE="${MODE_FILE:-$STATE_DIR/mode}"
# name:host-port:seconds-to-reconnect-after-wake (LIVE waits for the 20:35 weekly touch)
API_OFF_CONTAINERS="${API_OFF_CONTAINERS:-quant-dashboard-live-docker:18081:3600 quant-dashboard-docker:18080:900}"
API_FLAG="/data/api-off"
API_VERIFY_TIMEOUT="${API_VERIFY_TIMEOUT:-90}"

mkdir -p "$HOLD_DIR" "$STATE_DIR"

log() { echo "[$(TZ=Asia/Hong_Kong date '+%Y-%m-%d %H:%M:%S') HKT] $*" >> "$LOG"; }
say() { local p=""; [ "$DRY_RUN" = 1 ] && p="DRY-RUN (nothing changed): "; echo "$p$*"; log "$p$*"; }
push() { [ "$DRY_RUN" = 1 ] && { log "dry-run: would push $1: $2"; return 0; }
         [ -x "$PUSH" ] || [ -f "$PUSH" ] || return 0
         bash "$PUSH" "$1" "weekend-sleep" "$2" >/dev/null 2>&1 || true; }
hkt() { TZ=Asia/Hong_Kong date -d "@$1" '+%a %Y-%m-%d %H:%M HKT'; }
now_epoch() { echo "${NOW_EPOCH:-$(date +%s)}"; }

# -> "in_window week_key sleep_epoch wake_epoch next_wake_epoch"
window() {
    python3 - "$CONFIG" "$1" <<'PY'
import json, sys, datetime as dt
cfg = {"sleepStartDay": "Saturday", "sleepStartTime": "06:30",
       "wakeDay": "Sunday", "wakeTime": "20:00"}
try:
    with open(sys.argv[1], encoding="utf-8-sig") as f:
        j = json.load(f)
    for k in cfg:
        if str(j.get(k) or "").strip():
            cfg[k] = str(j[k]).strip()
except Exception:
    pass
HKT = dt.timezone(dt.timedelta(hours=8))           # no DST in Hong Kong
now = dt.datetime.fromtimestamp(int(sys.argv[2]), HKT)
days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
monday = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
def at(day, hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return monday + dt.timedelta(days=days.index(day.capitalize()), hours=h, minutes=m)
s, w = at(cfg["sleepStartDay"], cfg["sleepStartTime"]), at(cfg["wakeDay"], cfg["wakeTime"])
if w <= s:
    raise SystemExit("weekend-docker: wake must be after sleep inside one Mon-Sun week")
nxt = w if now < w else w + dt.timedelta(days=7)
print(int(s <= now < w), w.strftime("%Y-%m-%d"), int(s.timestamp()), int(w.timestamp()),
      int(nxt.timestamp()))
PY
}

running() { [ "$("$DOCKER" inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
health() { "$DOCKER" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null; }
hold_reason() { [ -f "$HOLD_DIR/$1" ] && head -c 200 "$HOLD_DIR/$1"; }
weekend_held() { hold_reason "$1" | grep -q "^$WEEKEND_TAG"; }
foreign_held() { [ -f "$HOLD_DIR/$1" ] && ! weekend_held "$1"; }
mem_line() { free -m 2>/dev/null | awk '/^Mem:/ {printf "used=%sMB buff/cache=%sMB available=%sMB", $3, $6, $7}'; }

# ---- display mode (QUANT_WEEKEND_DISPLAY_SPEC) -----------------------------------------
weekend_mode() {
    local m="${WEEKEND_MODE:-}"
    [ -z "$m" ] && [ -f "$MODE_FILE" ] && m=$(tr -d ' \r\n' < "$MODE_FILE")
    [ "$m" = display ] && echo display || echo sleep
}
is_display_dash() { local s; for s in $API_OFF_CONTAINERS; do [ "${s%%:*}" = "$1" ] && return 0; done; return 1; }
# containers this mode stops + holds
stop_set() {
    local c
    for c in $CONTAINERS; do
        if [ "$(weekend_mode)" = display ] && is_display_dash "$c"; then continue; fi
        echo "$c"
    done
}
flag_present() { "$DOCKER" exec "$1" test -f "$API_FLAG" >/dev/null 2>&1; }
flag_write() {   # $1 container, $2 json
    printf '%s\n' "$2" | "$DOCKER" exec -i "$1" sh -c "cat > $API_FLAG.tmp && mv $API_FLAG.tmp $API_FLAG" >/dev/null 2>&1
}
flag_clear() { "$DOCKER" exec "$1" rm -f "${API_FLAG:?}" >/dev/null 2>&1; }
# field from the dashboard's /status JSON ("" when unreachable / missing)
status_field() {   # $1 port, $2 field
    "$CURL" -fsS -m 10 "http://localhost:$1/status" 2>/dev/null | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin).get(sys.argv[1])
    print("" if v is None else (str(v).lower() if isinstance(v, bool) else v))
except Exception:
    print("")' "$2"
}
wait_api() {   # $1 port, $2 want (off|on) -> 0 when /status reports it within the timeout
    local t=0
    while :; do
        [ "$(status_field "$1" api_mode)" = "$2" ] && return 0
        [ "$t" -ge "$API_VERIFY_TIMEOUT" ] && return 1
        sleep 5; t=$((t + 5))
    done
}
wait_running() {   # $1 container
    local t=0 h
    while :; do
        if running "$1"; then h=$(health "$1"); case "$h" in healthy|none) return 0 ;; esac; fi
        [ "$t" -ge "$WAKE_TIMEOUT" ] && return 1
        sleep 5; t=$((t + 5))
    done
}

# Put every display dashboard into API-off. Idempotent; called by every in-window tick.
display_on() {   # $1 = wake epoch (flag `until`), $2 = week key
    local until=$1 wk=$2 s c port json wrote=() fb=() now
    now=$(now_epoch)
    json="{\"reason\": \"weekend\", \"since\": $now, \"until\": $until, \"by\": \"weekend-docker.sh\"}"
    for s in $API_OFF_CONTAINERS; do
        c=${s%%:*}; port=$(echo "$s" | cut -d: -f2)
        foreign_held "$c" && continue
        if weekend_held "$c"; then
            [ -f "$STATE_DIR/fallback-$c-$wk" ] && continue      # slept after a failed attempt
            # migration from sleep mode: release the weekend hold and start it into API-off
            if [ "$DRY_RUN" = 1 ]; then log "dry-run: would release $c and start it API-off"; continue; fi
            rm -f "${HOLD_DIR:?}/${c:?}"
            running "$c" || "$DOCKER" start "$c" >/dev/null 2>&1 || log "display: docker start $c failed"
            wait_running "$c" || log "display: $c not healthy after start"
        fi
        running "$c" || continue                          # stopped & unheld: the watchdogs own it
        flag_present "$c" && continue
        if [ "$DRY_RUN" = 1 ]; then log "dry-run: would write $API_FLAG in $c"; continue; fi
        flag_write "$c" "$json" || log "display: writing $API_FLAG in $c failed"
        if [ -n "$port" ] && ! wait_api "$port" off; then
            # Not confirmed (old image without api_mode, or unreachable): fall back to the old
            # behaviour for the rest of this weekend -- hold + stop it.
            touch "$STATE_DIR/fallback-$c-$wk"
            echo "$WEEKEND_TAG $(TZ=Asia/Hong_Kong date '+%Y-%m-%d %H:%M:%S') HKT until $(hkt "$until") (display fallback; QUANT_WEEKEND_DISPLAY_SPEC)" > "$HOLD_DIR/$c"
            "$DOCKER" stop -t "$STOP_TIMEOUT" "$c" >/dev/null 2>&1 || log "display: docker stop $c failed"
            fb+=("$c")
            continue
        fi
        wrote+=("$c")
    done
    [ ${#wrote[@]} -gt 0 ] && { say "display: broker API off in ${wrote[*]} until $(hkt "$until") (dashboards stay up)"
        push info "quant weekend display mode: ${wrote[*]} up with broker API off until $(hkt "$until")"; }
    [ ${#fb[@]} -gt 0 ] && { say "display: PROBLEM -- ${fb[*]} did not confirm api_mode=off; held + stopped instead"
        push critical "quant weekend display mode FAILED for ${fb[*]}: no api_mode=off on /status; stopped them instead (weekend sleep)"; }
    return 0
}

# Remove every flag (outside the window, or a manual wake). Then expect a reconnect.
display_off() {
    local s c port secs cleared=() now
    now=$(now_epoch)
    for s in $API_OFF_CONTAINERS; do
        c=${s%%:*}; port=$(echo "$s" | cut -d: -f2); secs=$(echo "$s" | cut -d: -f3)
        running "$c" || continue
        flag_present "$c" || continue
        if [ "$DRY_RUN" = 1 ]; then log "dry-run: would remove $API_FLAG in $c"; continue; fi
        flag_clear "$c" || log "display: removing $API_FLAG in $c failed"
        cleared+=("$c")
        echo "$((now + ${secs:-900})) $port" > "$STATE_DIR/verify-$c"
    done
    [ ${#cleared[@]} -gt 0 ] && say "display: broker API back on in ${cleared[*]}"
    return 0
}

# After display_off: each dashboard must show a fresh broker read (/status ok=true) by its
# deadline, else one critical push. Level-triggered across ticks.
verify_reconnect() {
    local f c deadline port now ok
    now=$(now_epoch)
    for f in "$STATE_DIR"/verify-*; do
        [ -f "$f" ] || continue
        c=${f##*/verify-}; read -r deadline port < "$f"
        ok=$(status_field "$port" ok)
        if [ "$ok" = true ]; then
            say "display: $c reconnected to the broker (fresh account read)"; rm -f "${f:?}"
        elif [ "$now" -ge "${deadline:-0}" ]; then
            say "display: PROBLEM -- $c not reconnected by $(hkt "$deadline") (/status ok=$ok)"
            push critical "quant weekend wake: $c has no fresh broker read by $(hkt "$deadline") -- check the gateway / api-off flag"
            rm -f "${f:?}"
        fi
    done
    return 0
}

# desired: echoes "asleep" or "awake"
desired() {
    local now=$1 in_win wk _s _w _n until
    read -r in_win wk _s _w _n < <(window "$now") || return 1
    if [ -f "$STATE_DIR/sleep-until" ]; then
        until=$(cat "$STATE_DIR/sleep-until" 2>/dev/null || echo 0)
        if [ "$now" -lt "${until:-0}" ]; then echo asleep; return; fi
    fi
    if [ "$in_win" = 1 ] && [ ! -f "$STATE_DIR/awake-$wk" ]; then echo asleep; else echo awake; fi
}

do_sleep() {   # $1 = wake epoch for the hold text
    local until_txt c newly=() restopped=() before after
    until_txt=$(hkt "$1")
    before=$(mem_line)
    for c in $(stop_set); do
        if foreign_held "$c"; then log "sleep: $c has a non-weekend hold ($(hold_reason "$c" | head -1)) -- left alone"; continue; fi
        if ! weekend_held "$c"; then
            if [ "$DRY_RUN" = 1 ]; then log "dry-run: would hold $c"
            else echo "$WEEKEND_TAG $(TZ=Asia/Hong_Kong date '+%Y-%m-%d %H:%M:%S') HKT until $until_txt (QUANT_WEEKEND_SLEEP_SPEC)" > "$HOLD_DIR/$c"; fi
            newly+=("$c")
        fi
        if running "$c"; then
            if [ "$DRY_RUN" = 1 ]; then log "dry-run: would stop $c"
            else "$DOCKER" stop -t "$STOP_TIMEOUT" "$c" >/dev/null 2>&1 || log "sleep: docker stop $c failed"; fi
            restopped+=("$c")
        fi
    done
    [ ${#newly[@]} -eq 0 ] && [ ${#restopped[@]} -eq 0 ] && return 0
    if [ ${#newly[@]} -eq 0 ]; then
        # Already asleep; something started a held container again (should not happen once
        # watchdog.ps1 honours holds). Re-stop quietly, log only.
        say "sleep: re-stopped ${restopped[*]} (was started while held)"
        return 0
    fi
    if [ "$DROP_CACHE" = 1 ] && [ "$DRY_RUN" != 1 ]; then
        sleep 5
        bash -c "$DROP_CACHE_CMD" >/dev/null 2>&1 || log "sleep: drop_caches failed (non-fatal)"
    fi
    after=$(mem_line)
    say "sleep: held ${newly[*]}; stopped ${restopped[*]:-none}; wake $until_txt. mem before: $before | after: $after"
    push info "quant weekend sleep: stopped ${restopped[*]:-nothing (already stopped)}; wake $until_txt. WSL mem $after"
}

do_wake() {
    local c woken=() bad=() t=0 h p
    for c in $CONTAINERS; do
        weekend_held "$c" || continue
        if [ "$DRY_RUN" = 1 ]; then log "dry-run: would release + start $c"; woken+=("$c"); continue; fi
        rm -f "$HOLD_DIR/$c"
        running "$c" || "$DOCKER" start "$c" >/dev/null 2>&1 || log "wake: docker start $c failed"
        woken+=("$c")
    done
    [ ${#woken[@]} -eq 0 ] && return 0
    [ "$DRY_RUN" = 1 ] && return 0
    while :; do
        bad=()
        for c in "${woken[@]}"; do
            if ! running "$c"; then bad+=("$c:not-running"); continue; fi
            h=$(health "$c"); case "$h" in healthy|none) ;; *) bad+=("$c:$h") ;; esac
        done
        [ ${#bad[@]} -eq 0 ] && break
        [ "$t" -ge "$WAKE_TIMEOUT" ] && break
        sleep 5; t=$((t + 5))
    done
    if [ ${#bad[@]} -eq 0 ] && [ -n "$HEALTH_PORTS" ]; then
        for p in $HEALTH_PORTS; do
            curl -fsS -m 10 -o /dev/null "http://localhost:$p/favicon.ico" 2>/dev/null || bad+=("port$p:no-answer")
        done
    fi
    if [ ${#bad[@]} -eq 0 ]; then
        say "wake: started ${woken[*]} -- all healthy after ${t}s. mem: $(mem_line)"
        push info "quant weekend wake: ${woken[*]} healthy after ${t}s"
    else
        say "wake: PROBLEM after ${t}s -- ${bad[*]}"
        push critical "quant weekend wake FAILED: ${bad[*]} (started ${woken[*]}; holds released, watchdogs now own them)"
    fi
}

tick() {
    local now want in_win wk s w nxt c need=0
    now=$(now_epoch)
    read -r in_win wk s w nxt < <(window "$now") || { log "tick: window() failed"; return 1; }
    # expired manual sleep
    if [ -f "$STATE_DIR/sleep-until" ] && [ "$now" -ge "$(cat "$STATE_DIR/sleep-until" 2>/dev/null || echo 0)" ]; then
        [ "$DRY_RUN" = 1 ] || rm -f "$STATE_DIR/sleep-until"
    fi
    want=$(desired "$now")
    if [ "$want" = asleep ]; then
        local until="$nxt"
        [ -f "$STATE_DIR/sleep-until" ] && until=$(cat "$STATE_DIR/sleep-until")
        for c in $(stop_set); do
            foreign_held "$c" && continue
            { weekend_held "$c" && ! running "$c"; } || need=1
        done
        [ "$need" = 1 ] && do_sleep "$until"
        [ "$(weekend_mode)" = display ] && display_on "$until" "$wk"
    else
        for c in $CONTAINERS; do weekend_held "$c" && need=1; done
        [ "$need" = 1 ] && do_wake
        display_off
        verify_reconnect
    fi
    return 0
}

status() {
    local now in_win wk s w nxt c
    now=$(now_epoch)
    read -r in_win wk s w nxt < <(window "$now")
    echo "now      $(hkt "$now")"
    echo "window   $(hkt "$s") -> $(hkt "$w")  (in window: $in_win)"
    echo "desired  $(desired "$now")"
    echo "mode     $(weekend_mode)  (display = dashboards stay up with the broker API off)"
    [ -f "$STATE_DIR/awake-$wk" ] && echo "override manual wake for this weekend ($wk)"
    [ -f "$STATE_DIR/sleep-until" ] && echo "override manual sleep until $(hkt "$(cat "$STATE_DIR/sleep-until")")"
    for c in $CONTAINERS; do
        printf '  %-30s running=%-5s hold=%s\n' "$c" "$(running "$c" && echo yes || echo no)" "$(hold_reason "$c" | head -1)"
    done
    local s port
    for s in $API_OFF_CONTAINERS; do
        c=${s%%:*}; port=$(echo "$s" | cut -d: -f2)
        running "$c" || continue
        printf '  %-30s api-off flag=%-3s /status api_mode=%s\n' "$c" \
            "$(flag_present "$c" && echo yes || echo no)" "$(status_field "$port" api_mode)"
    done
    echo "WSL mem  $(mem_line)"
}

cmd="${1:-status}"; shift || true
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

exec 9>"$STATE_DIR/lock"
if command -v flock >/dev/null 2>&1; then flock -w 300 9 || { log "$cmd: lock busy"; exit 0; }; fi

case "$cmd" in
    tick) tick ;;
    sleep)
        now=$(now_epoch); read -r _i _wk _s _w nxt < <(window "$now")
        echo "$nxt" > "$STATE_DIR/sleep-until"
        say "manual sleep until $(hkt "$nxt")"
        do_sleep "$nxt"
        [ "$(weekend_mode)" = display ] && display_on "$nxt" "$_wk" ;;
    wake)
        now=$(now_epoch); read -r _i wk _s _w _n < <(window "$now")
        rm -f "$STATE_DIR/sleep-until"; touch "$STATE_DIR/awake-$wk"
        find "$STATE_DIR" -name 'awake-*' -mtime +14 -delete 2>/dev/null
        say "manual wake (stays awake for the weekend of $wk)"
        do_wake
        display_off ;;
    status) status ;;
    *) echo "usage: $0 {tick [--dry-run]|sleep|wake|status}"; exit 2 ;;
esac
