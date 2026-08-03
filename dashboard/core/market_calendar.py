"""US (NYSE) trading-day awareness -- ADDED 2026-07-31, user-requested: the weekday-only
checks used elsewhere in this project (app.py::_market_open(), ib_exec.py::
within_entry_execution_window()) should also exclude US market holidays, not just
weekends. Uses `pandas_market_calendars` (NYSE calendar) rather than a hand-maintained
holiday list -- the whole point of excluding a maintenance burden is defeated by writing
code that itself needs annual updates, and a hardcoded fixed-rule list (nth-weekday-of-
month + Easter-relative Good Friday) would still miss one-off exceptions (e.g. a
market-wide closure for a national day of mourning) that a real, maintained exchange
calendar package already handles correctly.

Caches the NYSE trading-day SET per calendar year (a `nyse.schedule()` call is not free,
and this is checked on every LLM-refresh tick and every mirror_new() cycle -- both run
every ~1min, so an uncached call here would mean 1000+ real calendar computations/day for
no reason; the trading calendar for a given year never changes once published, so a
process-lifetime, per-year cache is safe and doesn't need invalidating)."""
from __future__ import annotations

import datetime as dt

from dashboard.core.log import log

_year_cache: dict[int, set] = {}


def _trading_days_for_year(year: int) -> set:
    if year not in _year_cache:
        try:
            import pandas_market_calendars as mcal
            nyse = mcal.get_calendar("NYSE")
            sched = nyse.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
            _year_cache[year] = set(sched.index.date)
        except Exception as e:                      # noqa: BLE001
            # Fails OPEN (treats every weekday as a trading day) rather than closed --
            # a missed holiday just means one wasted LLM call or one held-back entry that
            # will simply retry next cycle (harmless); failing CLOSED here could silently
            # block every single trading day if the package/network ever has a bad moment.
            log.warning("market_calendar: NYSE schedule fetch failed for %d, treating "
                       "every weekday as a trading day this year: %s", year, e)
            _year_cache[year] = None
    return _year_cache[year]


def is_us_trading_day(d: dt.date) -> bool:
    """True if `d` is a real NYSE trading day: not a weekend, not a US market holiday.
    Fails open (returns True for any weekday) if the calendar package itself is
    unavailable -- see _trading_days_for_year()'s docstring."""
    if d.weekday() >= 5:                              # Sat/Sun -- no calendar lookup needed
        return False
    trading_days = _trading_days_for_year(d.year)
    if trading_days is None:                          # fetch failed -- fail open
        return True
    return d in trading_days
