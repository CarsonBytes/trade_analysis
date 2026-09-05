"""Unit tests for dashboard/ops/gateway_window.py -- the weekend/holiday gate on the gateway
login watchdog.

Context (2026-09-05): scripts/gateway-login-watchdog.sh runs every minute, 24/7, and had no
calendar gate, while the scheduled relogin cron beside it is weekday-only. IBKR's weekly
server reset keeps the gateway logged out through much of the weekend, so the watchdog cycled
a relogin -- each one a phone push reading "APPROVE THE SECOND-FACTOR PROMPT" -- three times
an hour, all weekend. Measured in gateway-restart.log over 2026-08-29..31: 21 cycles Saturday,
52 Sunday, 31 Monday, versus 4-11 on a normal weekday.

Run:  uv run python -m dashboard.tests.test_gateway_window
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def _at(now_iso, is_open, next_open_iso, next_type="open"):
    from dashboard.ops import gateway_window
    now = dt.datetime.fromisoformat(now_iso)
    status = {"is_open": is_open,
              "next_change": dt.datetime.fromisoformat(next_open_iso) if next_open_iso else None,
              "next_change_type": next_type if next_open_iso else None}
    with mock.patch("dashboard.core.market_calendar.market_status", return_value=status):
        return gateway_window.should_be_quiet(now)


def test_quiet_through_the_weekend():
    print("should_be_quiet: the reported bug -- Saturday, with the next US open days away:")
    check("Saturday midday, next open Monday -> QUIET (this is the 2FA spam window)",
          _at("2026-09-05T04:45:00+00:00", False, "2026-09-07T13:30:00+00:00"), True)
    check("Sunday -> QUIET (52 relogin cycles fired on 2026-08-30)",
          _at("2026-09-06T10:00:00+00:00", False, "2026-09-07T13:30:00+00:00"), True)


def test_a_holiday_monday_is_also_quiet():
    print("\nshould_be_quiet: NOT a day-of-week rule. 2026-09-07 is Labor Day -- a Monday the "
          "US market is shut -- and a weekday rule would have spammed straight through it. "
          "The NYSE calendar already knows, so ask it:")
    check("Labor Day Monday, next open Tuesday -> QUIET",
          _at("2026-09-07T04:00:00+00:00", False, "2026-09-08T13:30:00+00:00"), True)


def test_active_whenever_a_session_is_within_reach():
    print("\nshould_be_quiet: the watchdog must still be the safety net it was built to be -- "
          "silence is only ever for hours nothing can be done with:")
    check("mid-session -> ACTIVE, always",
          _at("2026-09-08T15:00:00+00:00", True, None, None), False)
    check("1h before the open -> ACTIVE (the scheduled cron logs in 1.5h before)",
          _at("2026-09-08T12:30:00+00:00", False, "2026-09-08T13:30:00+00:00"), False)
    check("exactly at the lead boundary -> ACTIVE (boundary is inclusive of acting)",
          _at("2026-09-08T09:30:00+00:00", False, "2026-09-08T13:30:00+00:00"), False)
    check("just outside the lead -> QUIET",
          _at("2026-09-08T09:29:00+00:00", False, "2026-09-08T13:30:00+00:00"), True)


def test_fails_active_when_the_calendar_cannot_answer():
    print("\nshould_be_quiet: a broken calendar must NOT silently disable the watchdog -- a "
          "gateway logged out with nobody watching is the exact silent-outage class this "
          "project keeps hitting. A noisy phone beats an undetected outage:")
    check("no next_change at all -> ACTIVE",
          _at("2026-09-05T04:45:00+00:00", False, None, None), False)
    check("next change is a CLOSE, not an open -> ACTIVE (can't reason, so don't suppress)",
          _at("2026-09-05T04:45:00+00:00", False, "2026-09-08T20:00:00+00:00", "close"), False)


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except AssertionError:
                pass
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
