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
    assert ok, f"{name}: got {got!r} want {want!r}"


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
        # NOTE 2026-07-30: TECH_PAUSED no longer applies to the sleeve at all (see
        # test_sleeve_ignores_tech_paused_entirely() below) -- XLK reaching entry_signal here
        # regardless of TECH_PAUSED's value is expected, not something that needs patching off.
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


# ADDED 2026-07-30: user request -- tech-pause is now LOWER priority than the dipbuy-sleeve
# strategy, so a QQQ/XLK/SPY/EEM/ASHR dip-buy must still fire even while TECH_PAUSED=True.
# Regression guard for the fix that REMOVED the tech-pause check from place_sleeve_signals()
# entirely (it used to be the first check in the per-ticker loop, same as the core funnel).
def test_sleeve_ignores_tech_paused_entirely():
    print("place_sleeve_signals(): a QQQ entry reaches entry_signal() even with "
          "TECH_PAUSED=True -- the sleeve no longer checks it at all:")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper, sleeve
        check("resolves to the temp path", str(paper._DB), path)

        with mock.patch.object(sleeve, "sleeve_enabled", return_value=True), \
             mock.patch.object(sleeve.paper, "sleeve_active", return_value=True), \
             mock.patch.object(sleeve.paper, "TECH_PAUSED", True), \
             mock.patch.object(sleeve, "active_sleeve_universe", return_value=["QQQ"]), \
             mock.patch.object(sleeve, "_record_first_active_if_needed"), \
             mock.patch.object(sleeve, "_throttled", return_value=False), \
             mock.patch.object(sleeve, "entry_signal") as mock_entry:
            mock_entry.return_value = {
                "instrument": "PLACEHOLDER", "entry": 660.0, "sl": 630.0, "tp": 690.0,
                "risk_pct": 0.005, "vix_at_entry": 20.0, "asof": None,
                "rationale": "test signal",
            }
            logs = sleeve.place_sleeve_signals(equity_usd=100_000.0)
        check("QQQ reached entry_signal despite TECH_PAUSED=True", mock_entry.called, True)
        check("QQQ actually got placed", len(logs) == 1 and "QQQ" in logs[0], True)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


# ADDED 2026-07-31: entry_signal()'s VIX-spike condition was DROPPED per user-approved
# ablation (meanrev_filter_ablation_test.py's variant B beat the VIX-inclusive spec on
# CAGR/Sharpe/Calmar at every weight, IS and OOS -- see HANDOFF.md). Direct tests of the
# real function (not mocked), confirming the actual new gate, not just that nothing broke.
def _daily(close=97.0, ma20=100.0, rsi14=30.0, adx14=30.0, vix=15.0, vix_5ago=15.0):
    import pandas as pd
    return {"close": close, "vix": vix, "vix_5ago": vix_5ago, "ma5": close * 0.99,
           "ma20": ma20, "rsi14": rsi14, "adx14": adx14, "asof": pd.Timestamp.now()}


def test_entry_signal_fires_without_a_vix_spike():
    print("entry_signal(): fires on close/RSI/ADX alone -- flat VIX (no spike) no longer "
          "blocks entry (the 2026-07-31 change):")
    from dashboard.core import sleeve
    with mock.patch.object(sleeve, "_load_daily",
                           return_value=_daily(vix=15.0, vix_5ago=15.0)):  # 0% VIX move
        cand = sleeve.entry_signal("SPY")
    check("candidate returned despite no VIX spike", cand is not None, True)
    check("instrument correct", cand["instrument"] if cand else None, "SPY")


def test_entry_signal_still_requires_rsi_below_35():
    print("\nentry_signal(): RSI>=35 still blocks entry (unchanged):")
    from dashboard.core import sleeve
    with mock.patch.object(sleeve, "_load_daily", return_value=_daily(rsi14=40.0)):
        cand = sleeve.entry_signal("SPY")
    check("no candidate -- RSI too high", cand, None)


def test_entry_signal_requires_adx_above_25():
    print("\nentry_signal(): ADX<=25 blocks entry -- this is the filter that's actually "
          "earning its keep per the ablation, threshold RAISED 20->25 same day (a follow-up "
          "sweep found a better risk-adjusted tradeoff, see HANDOFF.md):")
    from dashboard.core import sleeve
    with mock.patch.object(sleeve, "_load_daily", return_value=_daily(adx14=15.0)):
        cand = sleeve.entry_signal("SPY")
    check("no candidate -- ADX too weak", cand, None)
    with mock.patch.object(sleeve, "_load_daily", return_value=_daily(adx14=22.0)):
        cand = sleeve.entry_signal("SPY")
    check("no candidate -- ADX=22 would have passed the OLD >20 threshold but not the new "
          ">25 one (confirms the threshold actually moved, not just 'ADX filtering exists')",
          cand, None)
    with mock.patch.object(sleeve, "_load_daily", return_value=_daily(adx14=26.0)):
        cand = sleeve.entry_signal("SPY")
    check("ADX=26 clears the new threshold -- candidate returned", cand is not None, True)


def test_entry_signal_still_requires_close_below_20ma():
    print("\nentry_signal(): close not below 20MA*0.975 still blocks entry (unchanged):")
    from dashboard.core import sleeve
    with mock.patch.object(sleeve, "_load_daily", return_value=_daily(close=100.0, ma20=100.0)):
        cand = sleeve.entry_signal("SPY")
    check("no candidate -- not oversold vs 20MA", cand, None)


def test_entry_signal_vix_still_drives_sizing_not_gating():
    print("\nentry_signal(): VIX>30 still bumps risk_pct to RISK_HIGH (sizing untouched, "
          "only the ENTRY gate on VIX was removed):")
    from dashboard.core import sleeve
    with mock.patch.object(sleeve, "_load_daily", return_value=_daily(vix=35.0, vix_5ago=35.0)):
        cand = sleeve.entry_signal("SPY")
    check("candidate returned", cand is not None, True)
    check("risk_pct bumped to RISK_HIGH", cand["risk_pct"] if cand else None, sleeve.RISK_HIGH)


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
