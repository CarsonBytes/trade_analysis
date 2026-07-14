"""Telegram alerting for key events -- ADDED 2026-07-14, after a session full of real
events (a false -89.8% drawdown display, an orphaned real broker order, a reconcile
mismatch, a portfolio-cap breach) that were each only discovered by a human happening to
check the right place. This is the push-notification side; core/notable_events.py is the
paired local changelog side -- both fire from the SAME call sites so they can't drift out
of sync with each other.

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


def is_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(message: str, level: str = "info") -> bool:
    """Send a Telegram alert. Returns True if actually sent (False if not configured,
    de-duped, or the send failed) -- callers should treat this as best-effort, never as a
    guarantee, and must never let a failure here break whatever real trading/monitoring
    logic triggered the alert in the first place."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.debug("notify: TELEGRAM_BOT_TOKEN/CHAT_ID not set, skipping alert: %s", message)
        return False
    now = time.time()
    last = _last_sent.get(message)
    if last is not None and (now - last) < _COOLDOWN_SEC:
        return False        # identical message within cooldown, skip
    _last_sent[message] = now
    try:
        import requests
        emoji = {"warning": "⚠️", "error": "\U0001f6a8"}.get(level, "ℹ️")
        mode = os.environ.get("DASH_FIXED_MODE", "?").upper()
        text = f"{emoji} [{mode}] {message}"
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("notify: Telegram API returned %s: %s",
                       resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:                     # noqa: BLE001 -- alerting must never raise
        log.warning("notify: failed to send Telegram alert: %s", e)
        return False
