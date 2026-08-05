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
import time

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


# ADDED 2026-08-05, user-requested: "time until next market open/close" for the dashboard's
# System health strip. Deliberately a SEPARATE cache/function from is_us_trading_day() above
# -- that one only needs a whole-day yes/no SET (cheap to hold in memory for a whole
# calendar year); this needs exact per-day open/close TIMESTAMPS, which `nyse.schedule()`
# already returns directly (real NYSE hours, e.g. 13:30-20:00 UTC most days) -- including
# early-close days (day before Thanksgiving, July 3rd, Christmas Eve when it falls on a
# weekday) for free, with no hardcoded 9:30am/4:00pm ET assumption anywhere in this module.
_status_cache: dict = {"ts": 0.0, "for_date": None, "data": None}
_STATUS_CACHE_SEC = 60.0   # this is read on every ~1min tick cycle; a fresh nyse.schedule()
                           # call every render would be wasteful for a value that only
                           # changes at the minute-to-minute resolution a countdown needs


def market_status(now: dt.datetime | None = None) -> dict:
    """{"is_open": bool, "next_change": aware UTC datetime | None, "next_change_type":
    "open"|"close"|None}. Fails to {"is_open": False, "next_change": None,
    "next_change_type": None} if the calendar package is unavailable -- a caller should
    treat that the same as "unknown," not "definitely closed" (see is_us_trading_day()'s
    fail-open reasoning; this one can't sensibly fail open with a concrete time, so it fails
    to "no answer" instead, which callers can render as blank rather than a wrong countdown)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cache_now = time.time()
    # `for_date` guards a caller passing an explicit `now` that jumps days between calls
    # (e.g. tests) from serving a stale cached answer within the 60s window -- in normal
    # (wall-clock) use this is essentially always a match, the TTL alone does the real work.
    if (cache_now - _status_cache["ts"] < _STATUS_CACHE_SEC
            and _status_cache["for_date"] == now.date()):
        return _status_cache["data"]
    empty = {"is_open": False, "next_change": None, "next_change_type": None}
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        # a 10-day forward window comfortably covers any holiday run (max real NYSE gap is
        # the Fri-before-Independence-Day-observed-Monday style 3-4 day weekend)
        sched = nyse.schedule(start_date=now.date().isoformat(),
                              end_date=(now.date() + dt.timedelta(days=10)).isoformat())
    except Exception as e:                            # noqa: BLE001
        log.warning("market_calendar: NYSE schedule fetch failed for market_status(): %s", e)
        return empty
    result = dict(empty)
    for market_open, market_close in zip(sched["market_open"], sched["market_close"]):
        o, c = market_open.to_pydatetime(), market_close.to_pydatetime()
        if o <= now < c:
            result = {"is_open": True, "next_change": c, "next_change_type": "close"}
            break
        if now < o:
            result = {"is_open": False, "next_change": o, "next_change_type": "open"}
            break
    _status_cache["ts"] = cache_now
    _status_cache["for_date"] = now.date()
    _status_cache["data"] = result
    return result
