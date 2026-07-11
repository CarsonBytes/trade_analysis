"""Targeted stress test: what did the CURRENT exact config (POS_CAP=0.25,
PORTFOLIO_CAP=1.0, RISK_PER_TRADE=0.01) actually do during specific historical crises,
isolated to that window -- not smoothed into a 6-window walk-forward average, which can
mask the single worst days inside a window (e.g. the 2006-2011 walk-forward window already
run this session covers GFC but blends it with 3 calmer years on either side).

Run:  uv run python -m dashboard.research.stress_test
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import pandas as pd
import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe

bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0
bt.CASH_YIELD = None

EVENTS = [
    ("2008 GFC",       "2007-09-01", "2009-03-31"),
    ("2020 COVID crash", "2020-02-01", "2020-05-31"),
    ("2022 rate-hike drawdown", "2022-01-01", "2022-12-31"),
]

print(f"Fetching full-history weekly data ({len(active_universe())} instruments)...")
cands = []
for inst in active_universe():
    df = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        continue
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].copy()
    df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    if len(df) < 220:
        continue
    cands += bt._signals(df, inst.key)

cands = sorted(cands, key=lambda c: c["entry_date"])
eq, realized = bt._portfolio(cands, 0.01)
eq = eq.sort_index()
print(f"{len(cands)} signals, full equity curve {eq.index[0].date()} to {eq.index[-1].date()}\n")

for label, start, end in EVENTS:
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    window = eq[(eq.index >= start_ts) & (eq.index <= end_ts)]
    if len(window) < 2:
        print(f"{label}: no equity points in window, skipping")
        continue
    # peak-to-trough WITHIN the window (not the all-time peak coming in -- this isolates
    # what happened DURING the event, not "how far below an already-distant high")
    running_peak = window.cummax()
    dd_series = (window - running_peak) / running_peak
    worst_dd = dd_series.min()
    worst_dd_date = dd_series.idxmin()
    total_ret = window.iloc[-1] / window.iloc[0] - 1
    # candidates with entry dates inside the window, for context (were we even trading?)
    n_entries = len([c for c in cands if start_ts <= c["entry_date"] <= end_ts])
    print(f"{label} ({start} to {end}):")
    print(f"  {len(window)} equity points | {n_entries} new entries during the window")
    print(f"  return over window: {total_ret*100:+.2f}%")
    print(f"  worst intra-window drawdown: {worst_dd*100:.2f}% (on {worst_dd_date.date()})")
    print()

print("NOTE: 'worst intra-window drawdown' is peak-to-trough using ONLY the window's own "
      "running peak -- it isolates what the strategy did DURING the crisis, distinct from "
      "the all-time maxDD figures quoted elsewhere (which might combine a pre-existing "
      "drawdown with a crisis, or might be set entirely outside these windows).")
