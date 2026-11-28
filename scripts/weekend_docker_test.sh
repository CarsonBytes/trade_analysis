#!/bin/bash
# Tests for scripts/weekend-docker.sh (QUANT_WEEKEND_SLEEP_SPEC section 5). Pure: a fake
# `docker` and a fake push script in a temp dir; no real container, no network, no Telegram.
# Run:  bash scripts/weekend_docker_test.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SUT="$HERE/weekend-docker.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
check() { if [ "$2" = "$3" ]; then echo "  PASS  $1"; pass=$((pass+1));
          else echo "  FAIL  $1: got [$2] want [$3]"; fail=$((fail+1)); fi; }

# --- fakes ---------------------------------------------------------------------------
cat > "$T/docker" <<'EOF'
#!/bin/bash
# state: $FAKE/<name>.run exists = running; $FAKE/calls = call log
echo "$*" >> "$FAKE/calls"
case "$1" in
  inspect)
    fmt="$3"; c="$4"
    case "$fmt" in
      *Running*) [ -f "$FAKE/$c.run" ] && echo true || echo false ;;
      *Health*)  if [ -f "$FAKE/$c.run" ]; then cat "$FAKE/$c.health" 2>/dev/null || echo healthy; else echo none; fi ;;
    esac ;;
  stop)  c="${@: -1}"; rm -f "$FAKE/$c.run" ;;
  start) c="$2"; [ "${FAKE_START_FAIL:-}" = "$c" ] && exit 1; touch "$FAKE/$c.run" ;;
  exec)  # the api-off flag lives at $FAKE/<c>.flag (survives stop/start, like the volume)
    shift; [ "$1" = "-i" ] && shift; c="$1"; shift
    [ -f "$FAKE/$c.run" ] || exit 1
    case "$*" in
      "test -f /data/api-off") [ -f "$FAKE/$c.flag" ] ;;
      "rm -f /data/api-off")   rm -f "${FAKE:?}/${c:?}.flag" ;;
      sh\ -c*)                 cat > "$FAKE/$c.flag" ;;
      *) exit 2 ;;
    esac ;;
esac
EOF
cat > "$T/curl" <<'EOF'
#!/bin/bash
# fake /status: port -> container via $FAKE/port-<port>; $FAKE/<c>.old = image without api_mode;
# $FAKE/<c>.connected = fresh broker read (ok=true)
url="${@: -1}"; port=$(echo "$url" | sed -E 's#.*:([0-9]+)/status#\1#')
c=$(cat "$FAKE/port-$port" 2>/dev/null) || exit 7
[ -f "$FAKE/$c.run" ] || exit 7
ok=false; [ -f "$FAKE/$c.connected" ] && ok=true
if [ -f "$FAKE/$c.old" ]; then echo "{\"ok\": $ok}"; exit 0; fi
mode=on; [ -f "$FAKE/$c.flag" ] && mode=off
echo "{\"ok\": $ok, \"api_mode\": \"$mode\"}"
EOF
cat > "$T/push" <<'EOF'
#!/bin/bash
echo "$1|$2|$3" >> "$FAKE/pushes"
EOF
chmod +x "$T/docker" "$T/push" "$T/curl"

C1=quant-dashboard-live-docker; C2=quant-dashboard-docker; C3=quant-ibgateway-docker
reset() {
    rm -rf "$T/fake" "$T/hold" "$T/state" "$T/log"; mkdir -p "$T/fake" "$T/hold" "$T/state"
    for c in $C1 $C2 $C3; do touch "$T/fake/$c.run"; done
    echo $C1 > "$T/fake/port-18081"; echo $C2 > "$T/fake/port-18080"
    : > "$T/fake/calls"; : > "$T/fake/pushes"
}
# HKT wall time -> epoch (independent of the box TZ)
ep() { TZ=Asia/Hong_Kong date -d "$1" +%s; }
run() {   # run <HKT time> <cmd...>
    local when="$1"; shift
    FAKE="$T/fake" NOW_EPOCH="$(ep "$when")" CONFIG="$T/none.json" HOLD_DIR="$T/hold" \
    STATE_DIR="$T/state" LOG="$T/log" DOCKER="$T/docker" PUSH="$T/push" HEALTH_PORTS="" \
    WAKE_TIMEOUT=0 STOP_TIMEOUT=1 DROP_CACHE=0 CURL="$T/curl" API_VERIFY_TIMEOUT=0 bash "$SUT" "$@"
}
drun() { WEEKEND_MODE=display run "$@"; }   # display mode (QUANT_WEEKEND_DISPLAY_SPEC)
flags() { ls "$T/fake"/*.flag 2>/dev/null | wc -l | tr -d ' '; }
isrun() { [ -f "$T/fake/$1.run" ] && echo yes || echo no; }
running_count() { ls "$T/fake"/*.run 2>/dev/null | wc -l | tr -d ' '; }
desired_at() { run "$1" status | awk '/^desired/ {print $2}'; }

echo "1. window math (default Sat 06:30 -> Sun 20:00 HKT), same answer in any box TZ:"
reset
for tz in Asia/Hong_Kong America/Toronto America/Los_Angeles UTC; do
    got=""
    for t in "2026-11-21 06:29" "2026-11-21 06:30" "2026-11-22 19:59" "2026-11-22 20:00" "2026-11-24 12:00"; do
        got="$got $(TZ=$tz desired_at "$t")"
    done
    check "TZ=$tz" "$got" " awake asleep asleep awake awake"
done
# Toronto DST ended Sun 2026-11-01 02:00 local; HKT window must not move.
check "DST weekend Sat 2026-10-31 06:30 HKT, box in Toronto" "$(TZ=America/Toronto desired_at '2026-10-31 06:30')" "asleep"
check "DST weekend Sun 2026-11-01 20:00 HKT, box in Toronto" "$(TZ=America/Toronto desired_at '2026-11-01 20:00')" "awake"
echo '{"sleepStartDay":"Saturday","sleepStartTime":"09:00","wakeDay":"Sunday","wakeTime":"21:00"}' > "$T/cfg.json"
check "config override honoured" "$(FAKE="$T/fake" NOW_EPOCH="$(ep '2026-11-21 08:00')" CONFIG="$T/cfg.json" HOLD_DIR="$T/hold" STATE_DIR="$T/state" LOG="$T/log" DOCKER="$T/docker" bash "$SUT" status | awk '/^desired/ {print $2}')" "awake"

echo "2. sleep holds before stopping; wake releases only weekend holds:"
reset
echo "parked 2026-11-21 16:37:34 HKT: Docker live login disabled" > "$T/hold/$C3"
rm -f "$T/fake/$C3.run"
run "2026-11-21 07:00" tick
check "two containers stopped" "$(running_count)" "0"
check "live hold is weekend-sleep" "$(cut -d' ' -f1 "$T/hold/$C1")" "weekend-sleep"
check "foreign hold untouched" "$(cut -d' ' -f1 "$T/hold/$C3")" "parked"
check "foreign-held container never started/stopped" "$(grep -c "$C3" "$T/fake/calls" | tr -d ' ')" "$(grep -c "inspect.*$C3" "$T/fake/calls" | tr -d ' ')"
check "one info push on sleep" "$(cut -d'|' -f1 "$T/fake/pushes" | tr '\n' ' ')" "info "
run "2026-11-22 20:05" tick
check "wake started the two" "$(running_count)" "2"
check "weekend holds removed" "$(ls "$T/hold")" "$C3"
check "foreign-held gateway still stopped" "$([ -f "$T/fake/$C3.run" ] && echo running || echo stopped)" "stopped"
check "info push on wake" "$(tail -1 "$T/fake/pushes" | cut -d'|' -f1)" "info"

echo "3. level-triggered: restarts mid-window are undone quietly; ticks are idempotent:"
reset
run "2026-11-21 07:00" tick
touch "$T/fake/$C1.run"                       # e.g. old watchdog.ps1 restarted it
run "2026-11-21 07:05" tick
check "re-stopped" "$(running_count)" "0"
check "no extra push for a re-stop" "$(wc -l < "$T/fake/pushes" | tr -d ' ')" "1"
run "2026-11-21 07:10" tick
check "idempotent tick: no further docker stop (3 + 1 re-stop)" "$(grep -c "^stop" "$T/fake/calls" | tr -d " ")" "4"
run "2026-11-22 21:00" tick                   # missed the exact wake minute
check "late tick still wakes" "$(running_count)" "3"

echo "4. overrides:"
reset
run "2026-11-21 07:00" tick
run "2026-11-21 10:00" wake >/dev/null
check "manual wake starts them" "$(running_count)" "3"
run "2026-11-21 10:05" tick; run "2026-11-22 08:00" tick
check "ticks leave them awake that weekend" "$(running_count)" "3"
run "2026-11-28 07:00" tick
check "next weekend sleeps again" "$(running_count)" "0"
run "2026-11-29 20:00" tick
reset
run "2026-11-25 12:00" sleep >/dev/null       # Wednesday manual sleep
check "manual sleep on a weekday" "$(running_count)" "0"
run "2026-11-26 12:00" tick
check "stays asleep until the next wake" "$(running_count)" "0"
run "2026-11-29 20:00" tick
check "wakes at Sunday 20:00" "$(running_count)" "3"
check "manual sleep override cleared" "$([ -f "$T/state/sleep-until" ] && echo present || echo gone)" "gone"

echo "7. wake failure pages critical:"
reset
run "2026-11-21 07:00" tick
FAKE_START_FAIL=$C2 run "2026-11-22 20:00" tick
check "critical push names the container" "$(tail -1 "$T/fake/pushes" | grep -o "critical.*$C2" | cut -d'|' -f1)" "critical"

echo "dry-run changes nothing:"
reset
run "2026-11-21 07:00" tick --dry-run
check "dry-run: all still running" "$(running_count)" "3"
check "dry-run: no holds" "$(ls "$T/hold" | wc -l | tr -d ' ')" "0"
check "dry-run: no push" "$(wc -l < "$T/fake/pushes" | tr -d ' ')" "0"
check "dry-run: logged intent" "$(grep -c 'dry-run: would stop' "$T/log" | tr -d ' ')" "3"

echo "D1. display mode: only the gateway sleeps; dashboards stay up with the API off:"
reset
drun "2026-11-21 07:00" tick
check "gateway stopped" "$(isrun $C3)" "no"
check "gateway held" "$(cut -d' ' -f1 "$T/hold/$C3")" "weekend-sleep"
check "live dashboard still running" "$(isrun $C1)" "yes"
check "paper dashboard still running" "$(isrun $C2)" "yes"
check "no hold on dashboards" "$(ls "$T/hold")" "$C3"
check "both flags written" "$(flags)" "2"
check "flag until = Sun 20:00 HKT" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["until"])' "$T/fake/$C1.flag")" "$(ep '2026-11-22 20:00')"
check "info pushes only (sleep + display)" "$(cut -d'|' -f1 "$T/fake/pushes" | sort -u | tr '\n' ' ')" "info "

echo "D2. level-triggered: a deleted flag is rewritten, ticks are idempotent:"
rm -f "${T:?}/fake/${C2:?}.flag"
drun "2026-11-21 07:05" tick
check "flag rewritten" "$(flags)" "2"
n=$(grep -c "^exec -i" "$T/fake/calls" | tr -d ' ')
drun "2026-11-21 07:10" tick
check "idempotent: no extra flag write" "$(grep -c "^exec -i" "$T/fake/calls" | tr -d ' ')" "$n"
check "dashboards never stopped" "$(grep -c "^stop.*dashboard" "$T/fake/calls" | tr -d ' ')" "0"

echo "D3. wake: gateway back first, flags removed, reconnect verified:"
drun "2026-11-22 20:00" tick
check "gateway started" "$(isrun $C3)" "yes"
check "flags removed" "$(flags)" "0"
check "verify pending for both" "$(ls "$T/state" | grep -c '^verify-' | tr -d ' ')" "2"
touch "$T/fake/$C2.connected"
drun "2026-11-22 20:05" tick
check "paper verified, live still pending" "$(ls "$T/state" | grep '^verify-' | tr '\n' ' ')" "verify-$C1 "
drun "2026-11-22 21:05" tick                  # live deadline (wake + 1h) passed, not connected
check "live not reconnected -> critical" "$(tail -1 "$T/fake/pushes" | grep -o "^critical.*$C1" | cut -d'|' -f1)" "critical"
check "verify cleared after paging" "$(ls "$T/state" | grep -c '^verify-' | tr -d ' ')" "0"

echo "D4. an image without api_mode falls back to the old sleep for that weekend:"
reset
touch "$T/fake/$C1.old"
drun "2026-11-21 07:00" tick
check "old-image live dashboard stopped" "$(isrun $C1)" "no"
check "and held" "$(cut -d' ' -f1 "$T/hold/$C1")" "weekend-sleep"
check "critical push names it" "$(grep -c "^critical.*$C1" "$T/fake/pushes" | tr -d ' ')" "1"
check "paper dashboard still in display mode" "$(isrun $C2)" "yes"
drun "2026-11-21 07:05" tick
check "fallback sticks (not restarted)" "$(isrun $C1)" "no"
check "no repeat critical" "$(grep -c "^critical" "$T/fake/pushes" | tr -d ' ')" "1"
drun "2026-11-22 20:00" tick
check "fallback dashboard woken at 20:00" "$(isrun $C1)" "yes"

echo "D5. migration: dashboards already asleep from sleep mode are started into display:"
reset
run "2026-11-21 07:00" tick                   # old sleep mode
check "sleep mode stopped all three" "$(running_count)" "0"
drun "2026-11-21 07:05" tick                  # switch to display mid-weekend
check "dashboards started" "$(isrun $C1)$(isrun $C2)" "yesyes"
check "gateway still asleep" "$(isrun $C3)" "no"
check "dashboard holds released" "$(ls "$T/hold")" "$C3"
check "flags written" "$(flags)" "2"

echo "D6. manual wake in display mode clears flags for the weekend:"
reset
drun "2026-11-21 07:00" tick
drun "2026-11-21 10:00" wake >/dev/null
check "flags cleared" "$(flags)" "0"
check "gateway up" "$(isrun $C3)" "yes"
drun "2026-11-21 10:05" tick
check "stays awake (no flag rewritten)" "$(flags)" "0"

echo "D7. a flag left outside the window is removed by the next tick; mode file works:"
reset
echo display > "$T/state/mode"
echo '{}' > "$T/fake/$C2.flag"
run "2026-11-24 12:00" tick
check "stale flag removed" "$(flags)" "0"
check "status shows mode from file" "$(run "2026-11-24 12:00" status | awk '/^mode/ {print $2}')" "display"

echo "5. watchdog.ps1 consults the hold before restarting a dashboard (static):"
WD="$HERE/../watchdog.ps1"
check "Get-HeldContainers defined" "$(grep -c '^function Get-HeldContainers' "$WD" | tr -d ' ')" "1"
check "hold dir matches infra-watchdog" "$(grep -c '/home/cap/.container-hold' "$WD" | awk '{print ($1>=1)}')" "1"
hold_line=$(grep -n '\$held = Get-HeldContainers' "$WD" | head -1 | cut -d: -f1)
restart_line=$(grep -n 'docker restart \$(\$inst.Container)' "$WD" | head -1 | cut -d: -f1)
check "hold check precedes docker restart" "$([ -n "$hold_line" ] && [ -n "$restart_line" ] && [ "$hold_line" -lt "$restart_line" ] && echo yes || echo no)" "yes"
check "stall_capture.sh skips a stopped dashboard" "$(grep -c 'State.Running' "$HERE/stall_capture.sh" | awk '{print ($1>=1)}')" "1"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
