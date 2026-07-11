"""Unit tests for core/sleeve.py's per-ticker circuit breaker (_ticker_breaker_tripped) --
tested in isolation before (never) and now end-to-end via place_sleeve_signals() itself, not
just the pure function. Uses an isolated temp sqlite db (never touches the real paper/live
journal), same pattern as test_reconcile.py.

Run:  uv run python -m dashboard.tests.test_sleeve
"""
from __future__ import annotations

import os
import tempfile
import datetime as dt
from unittest import mock

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


def _seed_trade(paper, instrument, method, realized_r, days_ago=10):
    """Insert a CLOSED trade directly, backdated so _recent_close()'s 60min cooldown
    doesn't also trigger and confuse what's being tested here."""
    exit_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).isoformat(
        timespec="seconds")
    t = paper.Trade(
        ts=exit_ts, instrument=instrument, direction="long", method=method,
        entry=100.0, sl=95.0, tp=103.0, rr=0.6, size_units=1.0,
        horizon_end=exit_ts, confidence=0.0, rationale="test",
        status=("WIN" if realized_r > 0 else "LOSS"), exit_ts=exit_ts,
        exit_price=100.0 + realized_r * 5, realized_r=realized_r)
    paper._insert(t)


def test_ticker_breaker_isolated():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper, sleeve
        check("resolves to the temp path", str(paper._DB), path)

        print("_ticker_breaker_tripped:")
        # SPY: 5 closed sleeve trades, mostly losing (1 win, 4 losses) -- should trip
        for r in [-1.0, -1.0, -1.0, -1.0, 0.6]:
            _seed_trade(paper, "SPY", sleeve.SLEEVE_METHOD, r)
        tripped = sleeve._ticker_breaker_tripped("SPY")
        check("bad-performing ticker (1/5 win, negative expR) -> tripped",
              tripped is not None, True)

        # QQQ: only 3 closed trades (below SLEEVE_BREAKER_MIN_N=5) -- not enough to judge yet
        for r in [-1.0, -1.0, -1.0]:
            _seed_trade(paper, "QQQ", sleeve.SLEEVE_METHOD, r)
        check("too few closed trades (n=3 < min 5) -> NOT tripped (can't judge yet)",
              sleeve._ticker_breaker_tripped("QQQ"), None)

        # XLK: 5 closed trades, GOOD performance -- should NOT trip
        for r in [1.0, 1.0, -1.0, 1.0, 1.0]:
            _seed_trade(paper, "XLK", sleeve.SLEEVE_METHOD, r)
        check("good-performing ticker (4/5 win, positive expR) -> NOT tripped",
              sleeve._ticker_breaker_tripped("XLK"), None)

        # a trade under a DIFFERENT method (e.g. the core book) must not count toward the
        # sleeve's own breaker -- methods are tracked independently
        for r in [-1.0, -1.0, -1.0, -1.0, -1.0]:
            _seed_trade(paper, "DIA", "ATR rr3.0", r)   # core method, NOT sleeve.SLEEVE_METHOD
        check("bad CORE trades on a ticker don't trip its SLEEVE breaker (methods isolated)",
              sleeve._ticker_breaker_tripped("DIA"), None)

        print("\nplace_sleeve_signals end-to-end (breaker actually skips the ticker):")
        with mock.patch.object(sleeve, "sleeve_enabled", return_value=True), \
             mock.patch.object(sleeve.paper, "sleeve_active", return_value=True), \
             mock.patch.object(sleeve, "active_sleeve_universe", return_value=["SPY", "XLK"]), \
             mock.patch.object(sleeve, "_record_first_active_if_needed"), \
             mock.patch.object(sleeve, "_throttled", return_value=False), \
             mock.patch.object(sleeve, "entry_signal") as mock_entry:
            mock_entry.return_value = {
                "instrument": "PLACEHOLDER", "entry": 100.0, "sl": 95.0, "tp": 103.0,
                "risk_pct": 0.005, "vix_at_entry": 20.0, "asof": None,
                "rationale": "test signal",
            }
            logs = sleeve.place_sleeve_signals(equity_usd=100_000.0)
        check("SPY (tripped breaker) never reaches entry_signal -> not in the call list",
              any("SPY" in str(c) for c in mock_entry.call_args_list), False)
        check("XLK (clean breaker) DOES reach entry_signal", mock_entry.called, True)
        check("only XLK actually got placed (SPY silently skipped)",
              len(logs) == 1 and "XLK" in logs[0], True)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    test_ticker_breaker_isolated()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
