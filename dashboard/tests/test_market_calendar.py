"""Unit tests for core/market_calendar.py's NYSE trading-day check -- ADDED 2026-07-31,
user-requested (exclude US market holidays from the weekday-only hours checks used
elsewhere: app.py::_market_open(), ib_exec.py::within_entry_execution_window()).

Run:  uv run python -m dashboard.tests.test_market_calendar
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


def test_is_us_trading_day_real_2026_dates():
    print("is_us_trading_day(): real NYSE 2026 dates (regular days, weekends, and every "
          "major holiday incl. a weekend-observed shift):")
    from dashboard.core import market_calendar
    cases = [
        ("2026-07-31 (regular Friday)", dt.date(2026, 7, 31), True),
        ("2026-08-01 (Saturday)", dt.date(2026, 8, 1), False),
        ("2026-08-02 (Sunday)", dt.date(2026, 8, 2), False),
        ("2026-01-01 (New Year's Day)", dt.date(2026, 1, 1), False),
        ("2026-01-19 (MLK Day)", dt.date(2026, 1, 19), False),
        ("2026-04-03 (Good Friday)", dt.date(2026, 4, 3), False),
        ("2026-05-25 (Memorial Day)", dt.date(2026, 5, 25), False),
        ("2026-06-19 (Juneteenth)", dt.date(2026, 6, 19), False),
        ("2026-07-03 (Independence Day observed, real Jul4 is a Saturday)",
         dt.date(2026, 7, 3), False),
        ("2026-09-07 (Labor Day)", dt.date(2026, 9, 7), False),
        ("2026-11-26 (Thanksgiving)", dt.date(2026, 11, 26), False),
        ("2026-12-25 (Christmas)", dt.date(2026, 12, 25), False),
    ]
    for label, d, want in cases:
        check(label, market_calendar.is_us_trading_day(d), want)


def test_year_cache_actually_caches():
    print("\n_trading_days_for_year(): caches per year -- second call for the same year "
          "doesn't re-fetch:")
    from dashboard.core import market_calendar
    market_calendar._year_cache.clear()
    with mock.patch("pandas_market_calendars.get_calendar") as mock_get_cal:
        mock_cal = mock.MagicMock()
        import pandas as pd
        mock_cal.schedule.return_value = pd.DataFrame(
            index=pd.DatetimeIndex([dt.date(2030, 6, 3)]))
        mock_get_cal.return_value = mock_cal
        market_calendar.is_us_trading_day(dt.date(2030, 6, 3))
        market_calendar.is_us_trading_day(dt.date(2030, 6, 4))
    check("get_calendar called exactly once for two lookups in the same year",
          mock_get_cal.call_count, 1)
    market_calendar._year_cache.clear()


def test_fails_open_on_calendar_fetch_error():
    print("\nis_us_trading_day(): fails OPEN (treats a weekday as tradeable) if the "
          "calendar package itself errors -- a missed holiday is harmless (one wasted "
          "LLM call/held-back entry), failing closed could silently block every weekday:")
    from dashboard.core import market_calendar
    market_calendar._year_cache.clear()
    with mock.patch("pandas_market_calendars.get_calendar", side_effect=RuntimeError("boom")):
        check("weekday still returns True despite the fetch error",
              market_calendar.is_us_trading_day(dt.date(2031, 3, 10)), True)  # a Monday
        check("weekend still correctly returns False (no calendar lookup needed at all)",
              market_calendar.is_us_trading_day(dt.date(2031, 3, 8)), False)  # a Saturday
    market_calendar._year_cache.clear()


def test_market_status_before_open_same_day():
    print("\nmarket_status(): before today's open -> closed, next_change = TODAY's open:")
    from dashboard.core import market_calendar
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)
    now = dt.datetime(2026, 8, 5, 10, 0, tzinfo=dt.timezone.utc)   # Wed, 4h30m before open
    ms = market_calendar.market_status(now)
    check("is_open", ms["is_open"], False)
    check("next_change_type", ms["next_change_type"], "open")
    check("next_change", ms["next_change"],
          dt.datetime(2026, 8, 5, 13, 30, tzinfo=dt.timezone.utc))
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)


def test_market_status_during_hours():
    print("\nmarket_status(): during regular hours -> open, next_change = TODAY's close:")
    from dashboard.core import market_calendar
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)
    now = dt.datetime(2026, 8, 5, 15, 0, tzinfo=dt.timezone.utc)   # Wed, mid-session
    ms = market_calendar.market_status(now)
    check("is_open", ms["is_open"], True)
    check("next_change_type", ms["next_change_type"], "close")
    check("next_change", ms["next_change"],
          dt.datetime(2026, 8, 5, 20, 0, tzinfo=dt.timezone.utc))
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)


def test_market_status_after_close_rolls_to_next_trading_day():
    print("\nmarket_status(): after today's close -> closed, next_change = the NEXT real "
          "trading day's open (not simply tomorrow):")
    from dashboard.core import market_calendar
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)
    now = dt.datetime(2026, 8, 5, 21, 0, tzinfo=dt.timezone.utc)   # Wed, 1h after close
    ms = market_calendar.market_status(now)
    check("is_open", ms["is_open"], False)
    check("next_change_type", ms["next_change_type"], "open")
    check("next_change (Thursday's open)", ms["next_change"],
          dt.datetime(2026, 8, 6, 13, 30, tzinfo=dt.timezone.utc))
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)


def test_market_status_skips_weekend_and_holiday():
    print("\nmarket_status(): a weekend AND a holiday in the way both get skipped in one "
          "step -- Sunday before Labor Day rolls straight to Tuesday's open (Mon = Labor Day, "
          "Sat/Sun = weekend), confirming holiday awareness, not just weekday-skipping:")
    from dashboard.core import market_calendar
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)
    now = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)   # Sunday before Labor Day
    ms = market_calendar.market_status(now)
    check("is_open", ms["is_open"], False)
    check("next_change (skips Labor Day Monday, lands on Tuesday)", ms["next_change"],
          dt.datetime(2026, 9, 8, 13, 30, tzinfo=dt.timezone.utc))
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)


def test_market_status_caches_within_ttl():
    print("\nmarket_status(): caches within the TTL -- a second call moments later for the "
          "same day doesn't re-fetch the schedule:")
    from dashboard.core import market_calendar
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)
    now = dt.datetime(2026, 8, 5, 15, 0, tzinfo=dt.timezone.utc)
    with mock.patch("pandas_market_calendars.get_calendar") as mock_get_cal:
        import pandas as pd
        mock_cal = mock.MagicMock()
        mock_cal.schedule.return_value = pd.DataFrame({
            "market_open": [pd.Timestamp("2026-08-05 13:30", tz="UTC")],
            "market_close": [pd.Timestamp("2026-08-05 20:00", tz="UTC")],
        })
        mock_get_cal.return_value = mock_cal
        market_calendar.market_status(now)
        market_calendar.market_status(now + dt.timedelta(seconds=5))
    check("get_calendar called exactly once for two calls within the TTL",
          mock_get_cal.call_count, 1)
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)


def test_market_status_fails_to_no_answer_on_calendar_fetch_error():
    print("\nmarket_status(): fails to a blank/no-answer result (not a wrong concrete time) "
          "if the calendar package errors:")
    from dashboard.core import market_calendar
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)
    with mock.patch("pandas_market_calendars.get_calendar", side_effect=RuntimeError("boom")):
        ms = market_calendar.market_status(
            dt.datetime(2026, 8, 5, 15, 0, tzinfo=dt.timezone.utc))
    check("is_open", ms["is_open"], False)
    check("next_change", ms["next_change"], None)
    check("next_change_type", ms["next_change_type"], None)
    market_calendar._status_cache.update(ts=0.0, for_date=None, data=None)


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
