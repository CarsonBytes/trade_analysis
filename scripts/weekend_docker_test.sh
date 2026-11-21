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
esac
EOF
cat > "$T/push" <<'EOF'
#!/bin/bash
echo "$1|$2|$3" >> "$FAKE/pushes"
EOF
chmod +x "$T/docker" "$T/push"

C1=quant-dashboard-live-docker; C2=quant-dashboard-docker; C3=quant-ibgateway-docker
reset() {
    rm -rf "$T/fake" "$T/hold" "$T/state" "$T/log"; mkdir -p "$T/fake" "$T/hold" "$T/state"
    for c in $C1 $C2 $C3; do touch "$T/fake/$c.run"; done
    : > "$T/fake/calls"; : > "$T/fake/pushes"
}
# HKT wall time -> epoch (independent of the box TZ)
ep() { TZ=Asia/Hong_Kong date -d "$1" +%s; }
run() {   # run <HKT time> <cmd...>
    local when="$1"; shift
    FAKE="$T/fake" NOW_EPOCH="$(ep "$when")" CONFIG="$T/none.json" HOLD_DIR="$T/hold" \
    STATE_DIR="$T/state" LOG="$T/log" DOCKER="$T/docker" PUSH="$T/push" HEALTH_PORTS="" \
    WAKE_TIMEOUT=0 STOP_TIMEOUT=1 DROP_CACHE=0 bash "$SUT" "$@"
}
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
