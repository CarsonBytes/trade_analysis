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


if __name__ == "__main__":
    test_is_us_trading_day_real_2026_dates()
    test_year_cache_actually_caches()
    test_fails_open_on_calendar_fetch_error()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
