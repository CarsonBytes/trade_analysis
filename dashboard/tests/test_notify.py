"""Unit tests for core/notify.py's Telegram alerting -- ADDED 2026-07-14.
Run:  uv run python -m dashboard.tests.test_notify
"""
from __future__ import annotations

import os
from unittest import mock

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


def test_is_configured():
    print("is_configured():")
    from dashboard.core import notify
    with mock.patch.dict(os.environ, {}, clear=False):
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)
        check("neither set -> False", notify.is_configured(), False)
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
        check("only token set -> False", notify.is_configured(), False)
        os.environ["TELEGRAM_CHAT_ID"] = "chat"
        check("both set -> True", notify.is_configured(), True)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)


def test_send_noop_when_not_configured():
    print("\nsend(): not configured -> no-op, returns False, never raises:")
    from dashboard.core import notify
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ.pop(k, None)
    notify._last_sent.clear()
    result = notify.send("test message")
    check("returns False", result, False)


def test_send_success_and_cooldown():
    print("\nsend(): configured -- sends once, then de-dups the IDENTICAL message within "
          "the cooldown window:")
    from dashboard.core import notify
    notify._last_sent.clear()
    calls = []

    class _FakeResp:
        status_code = 200
        text = "ok"

    def _fake_post(url, json, timeout):
        calls.append((url, json))
        return _FakeResp()

    with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok",
                                      "TELEGRAM_CHAT_ID": "chat"}), \
         mock.patch("requests.post", side_effect=_fake_post):
        r1 = notify.send("hello world")
        r2 = notify.send("hello world")     # identical message, should be de-duped
        r3 = notify.send("a different message")   # different message, should send
    check("first send succeeds", r1, True)
    check("identical message within cooldown is de-duped", r2, False)
    check("a different message still sends", r3, True)
    check("exactly 2 real HTTP calls made (not 3)", len(calls), 2)
    check("chat_id passed through correctly", calls[0][1]["chat_id"], "chat")


def test_send_handles_non_200_gracefully():
    print("\nsend(): non-200 response -> returns False, does not raise:")
    from dashboard.core import notify
    notify._last_sent.clear()

    class _FakeResp:
        status_code = 401
        text = "Unauthorized"

    with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok",
                                      "TELEGRAM_CHAT_ID": "chat"}), \
         mock.patch("requests.post", return_value=_FakeResp()):
        result = notify.send("will fail")
    check("returns False on non-200", result, False)


def test_send_handles_exception_gracefully():
    print("\nsend(): request raises -- returns False, does not propagate:")
    from dashboard.core import notify
    notify._last_sent.clear()

    with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok",
                                      "TELEGRAM_CHAT_ID": "chat"}), \
         mock.patch("requests.post", side_effect=ConnectionError("network down")):
        result = notify.send("network test")
    check("returns False, no exception propagated", result, False)


if __name__ == "__main__":
    test_is_configured()
    test_send_noop_when_not_configured()
    test_send_success_and_cooldown()
    test_send_handles_non_200_gracefully()
    test_send_handles_exception_gracefully()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
