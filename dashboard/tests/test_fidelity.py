"""Strategy fidelity F1 (IBKR_STRATEGY_FIDELITY_SPEC, 2026-11-21): live scores COMPLETED weekly
bars only -- the same bars the backtest scores -- and takes one funded entry per signal bar.

Measured reason (RETRO_PERFORMANCE_2026-11-21 §7.1): 16 of 81 LIVE signals existed only on the
in-progress weekly bar, at -0.68R avg vs -0.29R for the rest (~-11R).
No network: synthetic weekly series only.
Run:  uv run python -m dashboard.tests.test_fidelity
"""
from __future__ import annotations

from dashboard.tests import hermetic  # noqa: F401  (before any app module)

import os
import json
from unittest import mock

import numpy as np
import pandas as pd

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def _weekly(n=420, seed=7, start="2018-01-01"):
    """Monday-labelled weekly closes: a drifting random walk with regime switches."""
    rng = np.random.default_rng(seed)
    drift = np.where((np.arange(n) // 60) % 2 == 0, 0.004, -0.002)
    px = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.022, n)))
    idx = pd.date_range(start, periods=n, freq="W-MON")
    return pd.Series(px, index=idx, name="close")


def test_week_close_boundaries():
    print("week_close_utc(): a weekly bar is complete at its week's Friday close on its exchange:")
    from dashboard.data.providers import week_close_utc as wc
    check("US bar Mon 2026-11-16 -> Fri 16:00 ET = 21:00 UTC (EST)",
          wc(pd.Timestamp("2026-11-16")), pd.Timestamp("2026-11-20 21:00", tz="UTC"))
    check("US summer bar Mon 2026-07-13 -> Fri 16:00 EDT = 20:00 UTC",
          wc(pd.Timestamp("2026-07-13")), pd.Timestamp("2026-07-17 20:00", tz="UTC"))
    check("LSE bar -> Fri 16:30 London (GMT in Nov)",
          wc(pd.Timestamp("2026-11-16"), "LSEETF"), pd.Timestamp("2026-11-20 16:30", tz="UTC"))
    check("a Sunday label (MT5 W1) belongs to the FOLLOWING week",
          wc(pd.Timestamp("2026-11-15")), pd.Timestamp("2026-11-20 21:00", tz="UTC"))
    check("a tz-aware NY-midnight Monday label works the same",
          wc(pd.Timestamp("2026-11-16", tz="America/New_York")),
          pd.Timestamp("2026-11-20 21:00", tz="UTC"))
    check("a mid-week label (yfinance's in-progress row) -> that week's Friday",
          wc(pd.Timestamp("2026-11-18")), pd.Timestamp("2026-11-20 21:00", tz="UTC"))


def test_completed_weekly_trims_only_the_open_week():
    print("\ncompleted_weekly(): drops the in-progress week, never a completed one:")
    from dashboard.data.providers import completed_weekly
    s = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2026-11-02", "2026-11-09",
                                                         "2026-11-16"]))
    check("Fri 15:59 ET -> the Nov 16 week is still open, dropped",
          list(completed_weekly(s, pd.Timestamp("2026-11-20 20:59", tz="UTC"))), [1.0, 2.0])
    check("Fri 16:01 ET -> complete, kept",
          list(completed_weekly(s, pd.Timestamp("2026-11-20 21:01", tz="UTC"))), [1.0, 2.0, 3.0])
    check("Saturday -> all three complete",
          len(completed_weekly(s, pd.Timestamp("2026-11-21 12:00", tz="UTC"))), 3)
    check("Wednesday of the Nov 16 week -> dropped",
          len(completed_weekly(s, pd.Timestamp("2026-11-18 15:00", tz="UTC"))), 2)
    check("empty / None pass through", (completed_weekly(None), len(completed_weekly(s.iloc[:0]))),
          (None, 0))

    from dashboard.data import providers
    inst = mock.Mock(ib_exchange="")
    with mock.patch.dict(os.environ, {"SCORE_COMPLETED_WEEKS": "0"}):
        check("SCORE_COMPLETED_WEEKS=0 restores the old (untrimmed) behaviour",
              len(providers.scoring_series(inst, s, pd.Timestamp("2026-11-18", tz="UTC"))), 3)
    with mock.patch.dict(os.environ, {"SCORE_COMPLETED_WEEKS": "1"}):
        check("default on: trimmed", len(providers.scoring_series(
            inst, s, pd.Timestamp("2026-11-18", tz="UTC"))), 2)


def test_golden_live_scores_the_backtests_bars():
    print("\nGOLDEN: at any moment, live scores exactly the bar prefix the backtest scored for "
          "the most recently COMPLETED week -- and every backtest signal bar is a live signal:")
    import dashboard.research.backtest as bt
    from dashboard.core import paper
    from dashboard.core.scoring import score_from_facts
    from analyst.features import compute_facts
    from dashboard.data.providers import completed_weekly, week_close_utc

    key = "DIA"
    s = _weekly()
    df = pd.DataFrame({"open": s.shift(1).fillna(s.iloc[0]), "high": s * 1.01,
                       "low": s * 0.99, "close": s})
    df_utc = df.copy()
    df_utc.index = df_utc.index.tz_localize("UTC")
    sigs = bt._signals(df_utc, key)
    check("the synthetic series produces backtest signals to compare", len(sigs) >= 3, True)

    def live_view(now):
        # what live has on a weekday: every completed bar + the in-progress week's partial bar
        done = s[[week_close_utc(t) <= now for t in s.index]]
        nxt = s.index[len(done)] if len(done) < len(s) else None
        raw = pd.concat([done, pd.Series([float(s.iloc[len(done)]) * 0.97], index=[nxt])]) \
            if nxt is not None else done
        return completed_weekly(raw, now)

    mism = []
    for c in sigs:
        i = c["entry_i"]
        bar = s.index[i]
        # Wednesday of the NEXT week: bar i is the latest completed week, i+1 is in progress
        wed = week_close_utc(bar) + pd.Timedelta(days=5)
        lv = live_view(wed)
        if len(lv) != i + 1 or not lv.equals(s.iloc[: i + 1]):
            mism.append(("prefix", str(bar.date())))
            continue
        f_l, _ = compute_facts(lv, key)
        f_b, _ = compute_facts(s.iloc[: i + 1], key)
        sc = score_from_facts(key, f_l, "")
        rsi = f_l.get("rsi14") or 50.0
        live_ok = (sc.signal == "BUY" and sc.strength >= paper.MIN_STRENGTH
                   and not (paper.OVEREXT_FILTER and rsi > paper.OVEREXT_HI)) \
            if c["direction"] == "long" else sc.signal == "SELL"
        if f_l["last_price"] != f_b["last_price"] or f_l["atr14"] != f_b["atr14"] or not live_ok:
            mism.append(("signal", str(bar.date())))
    check("every backtest signal bar: same facts + same gated signal live", mism, [])
    one = sigs[0]["entry_i"]
    mid = week_close_utc(s.index[one]) - pd.Timedelta(days=2)   # Wednesday of that same week
    check("mid-week of a signal bar, live still sees only the PREVIOUS completed bar",
          len(live_view(mid)), one)


def test_score_one_uses_completed_bars_but_live_price():
    print("\nservice._score_one(): facts from completed bars, live price from the newest bar:")
    from dashboard.web import service
    s = _weekly(n=300)
    partial_idx = s.index[-1] + pd.Timedelta(days=7)
    raw = pd.concat([s, pd.Series([float(s.iloc[-1]) * 1.05], index=[partial_idx])])
    inst = mock.Mock(key="DIA", mt5="DIA", ib_exchange="")
    now = (partial_idx + pd.Timedelta(days=2)).tz_localize("UTC")   # Wednesday of that week
    with mock.patch.object(service, "get_history", return_value=(raw, "yfinance")), \
         mock.patch.object(service.mt5_client, "get_tick", return_value=None), \
         mock.patch("dashboard.data.providers.pd.Timestamp.now", return_value=now):
        key, score, src, live_px, *_ = service._score_one(inst)
    check("signal bar = last COMPLETED week", score.facts["signal_bar"], str(s.index[-1].date()))
    check("facts last_price = completed close", round(score.facts["last_price"], 6),
          round(float(s.iloc[-1]), 6))
    check("live price = the in-progress bar's price", round(live_px, 6), round(float(raw.iloc[-1]), 6))


def test_one_funded_entry_per_signal_bar():
    print("\npaper.signal_bar_taken(): one FUNDED entry per instrument per signal bar:")
    from dashboard.core.paper import signal_bar_taken as taken
    ef = lambda b: json.dumps({"signal_bar": b})                 # noqa: E731
    check("a WIN on the same bar blocks a second entry", taken([("WIN", ef("2026-11-16"))],
                                                                "2026-11-16"), True)
    check("a LOSS on the same bar blocks it", taken([("LOSS", ef("2026-11-16"))], "2026-11-16"),
          True)
    check("a never-filled CANCELLED attempt may retry", taken([("CANCELLED", ef("2026-11-16"))],
                                                             "2026-11-16"), False)
    check("a trade from an older bar doesn't block", taken([("LOSS", ef("2026-11-09"))],
                                                          "2026-11-16"), False)
    check("rows from before F1 (no signal_bar) don't block", taken([("WIN", "{}")], "2026-11-16"),
          False)
    check("no signal bar known -> never blocks", taken([("WIN", ef(None))], None), False)


def test_funding_order_non_equity_first_then_tightest_stop():
    print("\nC2 ib_exec.funding_order(): same week -> non-equity-index first, tightest stop first; "
          "an older week keeps its place:")
    from dashboard.execution.ib_exec import funding_order
    wk = lambda b: json.dumps({"signal_bar": b})                 # noqa: E731
    t = [  # id, instrument, entry, sl (stop %)
        {"id": 1, "instrument": "QQQ", "entry": 100.0, "sl": 97.0, "entry_facts": wk("2026-11-16")},
        {"id": 2, "instrument": "AMLP", "entry": 100.0, "sl": 97.3, "entry_facts": wk("2026-11-16")},
        {"id": 3, "instrument": "CPER", "entry": 100.0, "sl": 97.8, "entry_facts": wk("2026-11-16")},
        {"id": 4, "instrument": "DIA", "entry": 100.0, "sl": 98.4, "entry_facts": wk("2026-11-16")},
        {"id": 5, "instrument": "IWM", "entry": 100.0, "sl": 96.0, "entry_facts": wk("2026-11-09")},
    ]
    with mock.patch.dict(os.environ, {"FUNDING_PRIORITY": "1"}):
        check("order", [x["instrument"] for x in funding_order(t)],
              ["IWM", "CPER", "AMLP", "DIA", "QQQ"])
    with mock.patch.dict(os.environ, {"FUNDING_PRIORITY": "0"}):
        check("FUNDING_PRIORITY=0 keeps the journal order", [x["id"] for x in funding_order(t)],
              [1, 2, 3, 4, 5])


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
