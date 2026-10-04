"""Telegram alerting for key events -- ADDED 2026-07-14, after a session full of real
events (a false -89.8% drawdown display, an orphaned real broker order, a reconcile
mismatch, a portfolio-cap breach) that were each only discovered by a human happening to
check the right place. This is the push-notification side; core/notable_events.py is the
paired local changelog side -- both fire from the SAME call sites so they can't drift out
of sync with each other. Only CRITICAL level actually pushes to Telegram (see
_PUSH_LEVELS below) -- routine INFO events still land in the local changelog, just don't
buzz your phone, and WARNING/ERROR now roll up into the dashboard's daily digest
(docs/NOTIFICATION_SPEC.md, adopted here 2026-10-04).

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment (put them in
analyst/.env, or set them directly for whichever instance should alert). No-ops (logs at
debug, never raises) if not configured -- an instance that hasn't set this up yet behaves
exactly as before. To set up: message @BotFather on Telegram to create a bot and get a
token, then message your new bot once and check
https://api.telegram.org/bot<token>/getUpdates for your chat_id.
"""
from __future__ import annotations

import os
import time

from dashboard.core.log import log

_last_sent: dict[str, float] = {}
_COOLDOWN_SEC = 300     # de-dup: don't resend the EXACT same message within 5 minutes --
                        # cheap protection against a fast-repeating cycle spamming the
                        # same alert every 30s if something stays broken for a while


# ADDED 2026-08-26: optional second push channel -- ntfy.sh (or any self-hosted ntfy
# server). Telegram requires creating a bot + fetching a chat_id, which some devices/
# setups make awkward; ntfy is just an HTTP POST to a topic URL and its mobile app
# subscribes directly. Set NTFY_URL to a full topic URL (e.g. https://ntfy.sh/quant-
# <something-random>) and optionally NTFY_TOKEN for access-controlled topics. Same
# CRITICAL-only level filter and per-message cooldown as Telegram -- both channels fire
# from the same send(), so call sites never pick between them.
def _send_ntfy(message: str, level: str) -> bool:
    url = os.environ.get("NTFY_URL", "").strip()
    if not url:
        return False
    try:
        import requests
        headers = {
            "Title": f"[{os.environ.get('DASH_FIXED_MODE', '?').upper()}] "
                     f"{'WARNING' if level == 'warning' else 'ALERT'}",
            "Priority": "high" if level in ("error", "critical") else "default",
            "Tags": "rotating_light" if level in ("error", "critical") else "warning",
            # plain ASCII-safe tag fallback handled server-side by ntfy
        }
        token = os.environ.get("NTFY_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        if resp.status_code >= 300:
            log.warning("notify: ntfy returned %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:                     # noqa: BLE001 -- alerting must never raise
        log.warning("notify: failed to send ntfy alert: %s", e)
        return False


def is_configured() -> bool:
    return bool((os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
                or os.environ.get("NTFY_URL"))


# ADDED 2026-07-15: only important levels push to Telegram -- user asked for
# "important alert or notice" only. INFO-level events (new order placed, sleeve order
# placed, position closed -- the routine, happens-every-day stuff) still get recorded in
# the local changelog (notable_events.record() writes that regardless of this filter),
# just no longer buzz your phone for something that isn't actionable.
#
# TIGHTENED 2026-10-04 from {"warning", "error"} to {"critical"} -- see
# docs/NOTIFICATION_SPEC.md. Retro over the box's logs measured ~9 Telegram
# messages/day at the old setting, most of them describing events that had
# already resolved themselves. The decision rule is now uniform across every
# sender on this machine: *would ignoring this for 12 hours make it worse?*
# If no, it is not critical and it does not buzz the phone. WARNING/ERROR are
# still recorded (local changelog + the dashboard's daily digest) -- they are
# demoted, not discarded.
_PUSH_LEVELS = {"critical"}


def send(message: str, level: str = "info") -> bool:
    """Send an alert via every configured channel (Telegram and/or ntfy). Returns True if
    ANY channel actually sent (False if none configured, not an important-enough level,
    de-duped, or every send failed) -- callers should treat this as best-effort, never as
    a guarantee, and must never let a failure here break whatever real trading/monitoring
    logic triggered the alert in the first place."""
    if level not in _PUSH_LEVELS:
        return False         # routine/info -- local changelog only, no push
    now = time.time()
    last = _last_sent.get(message)
    if last is not None and (now - last) < _COOLDOWN_SEC:
        return False        # identical message within cooldown, skip
    _last_sent[message] = now
    sent_any = False
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            import requests
            emoji = {"warning": "⚠️", "error": "🚨",
                     "critical": "🚨"}.get(level, "ℹ️")
            mode = os.environ.get("DASH_FIXED_MODE", "?").upper()
            text = f"{emoji} [{mode}] {message}"
            # MIRROR 2026-09-30: record every outbound push where an operator can see it.
            # Telegram's getUpdates only returns INBOUND messages, so bot->user alerts are
            # structurally invisible to any polling check -- `docker logs | grep TG-PUSH`
            # (the tg_check poller mirrors these as `out:` lines) is the outbound record.
            log.info("notify: TG-PUSH [%s/%s] %s", mode, level, text)
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
            if resp.status_code != 200:
                log.warning("notify: Telegram API returned %s: %s",
                            resp.status_code, resp.text[:200])
            else:
                sent_any = True
        except Exception as e:                 # noqa: BLE001 -- alerting must never raise
            log.warning("notify: failed to send Telegram alert: %s", e)
    else:
        log.debug("notify: TELEGRAM_BOT_TOKEN/CHAT_ID not set, skipping Telegram: %s", message)
    # ntfy is attempted independently -- one channel being unconfigured/down must never
    # suppress the other (the whole point of a second channel is surviving the first's outage).
    if os.environ.get("NTFY_URL"):
        try:
            if _send_ntfy(message, level):
                sent_any = True
        except Exception as e:                 # noqa: BLE001
            log.warning("notify: ntfy dispatch failed unexpectedly: %s", e)
    return sent_any
