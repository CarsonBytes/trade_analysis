#!/bin/bash
# Push-notification helper for the gateway watchdog stack (2026-08-26). Same channels as
# dashboard/core/notify.py (Telegram + ntfy) but dependency-free bash, because it runs
# from cron OUTSIDE any Python process. Reads credentials from the environment first,
# then analyst/.env (rsynced to /home/cap/quant/analyst/.env by the deploy script).
# Best-effort by contract: a failed push must never break the calling watchdog.
set -uo pipefail

msg="${1:-gateway notification}"
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

if [ -n "$tg_token" ] && [ -n "$tg_chat" ]; then
    curl -sf -m 10 -X POST "https://api.telegram.org/bot${tg_token}/sendMessage" \
        -d chat_id="$tg_chat" -d text="⚠️ [${mode}] $msg" >/dev/null 2>&1 || true
fi
if [ -n "$ntfy_url" ]; then
    auth=""
    [ -n "$ntfy_tok" ] && auth="-H Authorization:Bearer\ $ntfy_tok"
    if [ -n "$ntfy_tok" ]; then
        curl -sf -m 10 -X POST "$ntfy_url" -H "Authorization: Bearer $ntfy_tok" \
            -H "Title: QTS [${mode}] gateway" -H "Priority: high" -d "$msg" >/dev/null 2>&1 || true
    else
        curl -sf -m 10 -X POST "$ntfy_url" \
            -H "Title: QTS [${mode}] gateway" -H "Priority: high" -d "$msg" >/dev/null 2>&1 || true
    fi
fi
exit 0
