#!/bin/bash
# Push-notification helper for the gateway watchdog stack (2026-08-26). Same channels as
# dashboard/core/notify.py (Telegram + ntfy) but dependency-free bash, because it runs
# from cron OUTSIDE any Python process. Reads credentials from the environment first,
# then analyst/.env (rsynced to /home/cap/quant/analyst/.env by the deploy script).
# Best-effort by contract: a failed push must never break the calling watchdog.
#
# NOTIFICATION_SPEC v1 (docs/NOTIFICATION_SPEC.md) -- 2026-10-04:
#   gateway-push.sh <level> <key> <message>
# Only `critical` reaches Telegram/ntfy. Everything else is appended to the
# shared audit trail (so the dashboard digest can report what did NOT page)
# and dropped. `key` is what makes a retry loop page once per INCIDENT rather
# than once per attempt -- the omission of exactly that is what turned 299
# relogin cycles into 299 phone buzzes.
# A bare single argument is still accepted and treated as critical, so an
# out-of-tree caller can never silently stop alerting.
set -uo pipefail

level="${2:-}"
if [ -n "$level" ] && [ $# -ge 3 ]; then
    key="$2"; msg="$3"
else
    level="critical"; key="legacy"; msg="${1:-gateway notification}"
fi
case "$level" in
    critical|error|warning|info) ;;
    *) level="critical" ;;
esac

envfile=/home/cap/quant/analyst/.env

tg_token="${TELEGRAM_BOT_TOKEN:-}"; tg_chat="${TELEGRAM_CHAT_ID:-}"
ntfy_url="${NTFY_URL:-}"; ntfy_tok="${NTFY_TOKEN:-}"
if [ -f "$envfile" ]; then
    [ -z "$tg_token" ] && tg_token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$envfile" | head -1 | cut -d= -f2-)
    [ -z "$tg_chat" ]  && tg_chat=$(grep -E '^TELEGRAM_CHAT_ID=' "$envfile" | head -1 | cut -d= -f2-)
    [ -z "$ntfy_url" ] && ntfy_url=$(grep -E '^NTFY_URL=' "$envfile" | head -1 | cut -d= -f2-)
    [ -z "$ntfy_tok" ] && ntfy_tok=$(grep -E '^NTFY_TOKEN=' "$envfile" | head -1 | cut -d= -f2-)
fi
mode=$(grep -E '^DASH_FIXED_MODE=' /home/cap/quant/.env 2>/dev/null | cut -d= -f2-)
[ -z "${mode:-}" ] && mode="?"

# --- shared delivery audit (spec 6) ------------------------------------------
# Same file the dashboard's alerts.py appends to, so one trail answers "was
# this actually delivered?" across every sender on the box.
AUDIT_DIR="${ALERT_AUDIT_DIR:-/mnt/d/claude/alerts/audit}"
AUDIT_FILE="$AUDIT_DIR/alerts.jsonl"
PUSH_STATE_DIR="${GATEWAY_PUSH_STATE:-/home/cap/.gateway-watchdog}"
COOLDOWN_KEY_SEC="${COOLDOWN_KEY_SEC:-900}"
CRITICAL_DAILY_CAP="${CRITICAL_DAILY_CAP:-10}"

json_escape() { printf '%s' "$1" | tr -d '"\\\r\n'; }
key_slug() { printf '%s' "$1" | tr -c 'A-Za-z0-9_.-' '_' ; }

# audit <outcome> <digest_eligible> [http_status] -- never fails the caller.
audit() {
    local outcome="$1" digest="$2" http="${3:-}"
    mkdir -p "$AUDIT_DIR" 2>/dev/null || true
    printf '{"ts":"%s","level":"%s","source":"gateway-watchdog","key":"%s","outcome":"%s","http_status":%s,"digest_eligible":%s,"mode":"%s","text":"%s"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$level" "$(json_escape "$key")" "$outcome" \
        "${http:-null}" "$digest" "$(json_escape "$mode")" "$(json_escape "$msg")" \
        >> "$AUDIT_FILE" 2>/dev/null || true
}

# Non-critical: recorded, never sent. This IS the digest feed.
if [ "$level" != "critical" ]; then
    if [ "$level" = "info" ]; then
        audit "suppressed-level" false
    else
        audit "suppressed-level" true
    fi
    exit 0
fi

mkdir -p "$PUSH_STATE_DIR" 2>/dev/null || true
slug=$(key_slug "$key")
now=$(date +%s)

# Per-key cooldown: one incident, one buzz.
if [ -f "$PUSH_STATE_DIR/push.$slug" ]; then
    last=$(tr -cd '0-9' < "$PUSH_STATE_DIR/push.$slug" 2>/dev/null)
    if [ -n "${last:-}" ] && [ $((now - last)) -lt "$COOLDOWN_KEY_SEC" ]; then
        audit "suppressed-cooldown-key" false
        exit 0
    fi
fi

# Hard daily cap -- the structural guarantee that no single bug floods chat.
datef=$(date +%Y%m%d)
if [ "$(cat "$PUSH_STATE_DIR/cap.date" 2>/dev/null)" != "$datef" ]; then
    printf '%s' "$datef" > "$PUSH_STATE_DIR/cap.date" 2>/dev/null || true
    printf '0' > "$PUSH_STATE_DIR/cap.count" 2>/dev/null || true
    rm -f "$PUSH_STATE_DIR/cap.done" 2>/dev/null || true
fi
cap_count=0
if [ -f "$PUSH_STATE_DIR/cap.count" ]; then
    cap_count=$(tr -cd '0-9' < "$PUSH_STATE_DIR/cap.count" 2>/dev/null)
    cap_count=${cap_count:-0}
fi
if [ "$cap_count" -ge "$CRITICAL_DAILY_CAP" ]; then
    if [ ! -f "$PUSH_STATE_DIR/cap.done" ]; then
        touch "$PUSH_STATE_DIR/cap.done" 2>/dev/null || true
        msg="+${CRITICAL_DAILY_CAP} more criticals suppressed today (cap reached) -- see the dashboard"
        key="cap:collapse"
    else
        audit "suppressed-cap" false
        exit 0
    fi
fi

tg_status=""
if [ -n "$tg_token" ] && [ -n "$tg_chat" ]; then
    tg_status=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST \
        "https://api.telegram.org/bot${tg_token}/sendMessage" \
        -d chat_id="$tg_chat" -d text="⚠️ [${mode}] $msg" 2>/dev/null || true)
else
    audit "not-configured" false
    exit 0
fi

if [ -n "$ntfy_url" ]; then
    if [ -n "$ntfy_tok" ]; then
        curl -sf -m 10 -X POST "$ntfy_url" -H "Authorization: Bearer $ntfy_tok" \
            -H "Title: QTS [${mode}] gateway" -H "Priority: high" -d "$msg" >/dev/null 2>&1 || true
    else
        curl -sf -m 10 -X POST "$ntfy_url" \
            -H "Title: QTS [${mode}] gateway" -H "Priority: high" -d "$msg" >/dev/null 2>&1 || true
    fi
fi

printf '%s' "$now" > "$PUSH_STATE_DIR/push.$slug" 2>/dev/null || true
printf '%s' "$((cap_count + 1))" > "$PUSH_STATE_DIR/cap.count" 2>/dev/null || true

if [ "$tg_status" = "200" ]; then
    audit "sent" false 200
else
    audit "failed" false "${tg_status:-}"
fi
exit 0
