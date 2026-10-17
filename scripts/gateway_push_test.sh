#!/bin/bash
# Offline test suite for gateway-push.sh -- NOTIFICATION_SPEC v1 level matrix.
# Safe by construction: info/error/warning never leave the box; critical cases are
# pre-seeded with a fresh cooldown stamp so they suppress BEFORE any network send;
# fake Telegram creds are exported as defense in depth. One audit line per case.
# Prints PASS/FAIL lines; exit code = number of failures.
set -u

REPO=/mnt/d/quant/scripts/gateway-push.sh
DEPLOYED=/home/cap/quant/scripts/gateway-push.sh
WORK=$(mktemp -d /mnt/d/quant/tmp/pushtest.XXXXXX)
AUDIT="$WORK/audit"
STATE="$WORK/state"
mkdir -p "$AUDIT" "$STATE"
AUDIT_FILE="$AUDIT/alerts.jsonl"
fails=0
npass=0

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

pass() { echo "PASS $1"; npass=$((npass + 1)); }
failf() { echo "FAIL $1 -- $2"; fails=$((fails + 1)); }

# run_case <label> <exp_level> <exp_outcome> <exp_digest> -- <push args...>
run_case() {
    local label="$1" exp_level="$2" exp_outcome="$3" exp_digest="$4"
    shift 4
    [ "${1:-}" = "--" ] && shift
    local before line
    before=$( (wc -l < "$AUDIT_FILE") 2>/dev/null || echo 0)
    TELEGRAM_BOT_TOKEN=FAKE TELEGRAM_CHAT_ID=FAKE \
        ALERT_AUDIT_DIR="$AUDIT" GATEWAY_PUSH_STATE="$STATE" \
        bash "$REPO" "$@" >/dev/null 2>&1
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        failf "$label" "exit=$rc (must always be 0)"
        return
    fi
    line=$(tail -n +"$((before + 1))" "$AUDIT_FILE" 2>/dev/null | head -1)
    if [ -z "$line" ]; then
        failf "$label" "no audit line appended"
        return
    fi
    local ok=1 why=""
    case "$line" in *"\"level\":\"$exp_level\""*) ;; *) ok=0; why="level expected=$exp_level line=$line";; esac
    case "$line" in *"\"outcome\":\"$exp_outcome\""*) ;; *) ok=0; why="${why:+$why; }outcome expected=$exp_outcome";; esac
    case "$exp_digest" in
        true)  case "$line" in *'"digest_eligible":true'*)  ;; *) ok=0; why="${why:+$why; }digest expected=true";; esac ;;
        false) case "$line" in *'"digest_eligible":false'*) ;; *) ok=0; why="${why:+$why; }digest expected=false";; esac ;;
    esac
    if [ "$ok" -eq 1 ]; then pass "$label"; else failf "$label" "$why"; fi
}

# 1) non-critical levels: audit-only, never sent, digest per spec.
run_case "info suppressed-level, not digest"   info    suppressed-level false -- info    infok "test info message"
run_case "error suppressed-level, digest"      error   suppressed-level true  -- error   errk "test error message"
run_case "warning suppressed-level, digest"    warning suppressed-level true  -- warning warnk "test warning message"

# 2) critical hits per-key cooldown (pre-seeded -> suppressed before any send).
printf '%s' "$(date +%s)" > "$STATE/push.coolkey"
run_case "critical suppressed-by-cooldown"     critical suppressed-cooldown-key false -- critical coolkey "test critical message"

# 3) legacy single-arg call still routes to critical (and is cooldown-suppressed).
printf '%s' "$(date +%s)" > "$STATE/push.legacy"
before=$( (wc -l < "$AUDIT_FILE") 2>/dev/null || echo 0)
TELEGRAM_BOT_TOKEN=FAKE TELEGRAM_CHAT_ID=FAKE \
    ALERT_AUDIT_DIR="$AUDIT" GATEWAY_PUSH_STATE="$STATE" \
    bash "$REPO" "test legacy message" >/dev/null 2>&1
rc=$?
line=$(tail -n +"$((before + 1))" "$AUDIT_FILE" 2>/dev/null | head -1)
if [ "$rc" -eq 0 ] && \
   case "$line" in *'"level":"critical"'*'"key":"legacy"'*'"outcome":"suppressed-cooldown-key"'*) true ;; *) false ;; esac
then
    pass "legacy 1-arg -> critical key=legacy, cooldown-suppressed"
else
    failf "legacy 1-arg" "rc=$rc line=$line"
fi

# 4) unknown level with 3 args coerces to critical (never silently dropped).
printf '%s' "$(date +%s)" > "$STATE/push.bogkey"
run_case "unknown level coerced to critical"   critical suppressed-cooldown-key false -- bogus bogkey "test bogus message"

# 5) syntax + deployed-copy identity.
if bash -n "$REPO" 2>/dev/null; then
    pass "repo gateway-push.sh syntax"
else
    failf "repo gateway-push.sh syntax" "bash -n failed"
fi
if [ "$(diff -q "$REPO" "$DEPLOYED" >/dev/null 2>&1; echo $?)" = "0" ]; then
    pass "deployed copy identical to repo"
else
    failf "deployed copy identical to repo" "diff: $(diff "$REPO" "$DEPLOYED" | head -3 | tr '\n' ' ')"
fi

echo "GATEWAY_PUSH_RESULT pass=$npass fails=$fails"
exit "$fails"
