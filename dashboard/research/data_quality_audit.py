"""Data-quality audit of the 30-year yfinance weekly history every backtest in this project
relies on. Nobody has ever checked this for suspicious gaps, zero-volume prints, or price
jumps that don't correspond to a known split -- garbage-in-garbage-out risk that's been
silently assumed away. Flags candidates for a human look; doesn't auto-fix anything.

Run:  uv run python -m dashboard.research.data_quality_audit
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import pandas as pd
import yfinance as yf

from dashboard.instruments import active_universe

JUMP_THRESHOLD = 0.25       # single-week |return| this large gets flagged unless a nearby
                             # split explains it
GAP_THRESHOLD_DAYS = 21      # a gap this long between consecutive weekly bars (mid-history,
                             # not just at the start) is suspicious for a liquid US ETF

print(f"Auditing {len(active_universe())} instruments...\n")
any_flags = False
for inst in active_universe():
    df = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        print(f"{inst.key:<6} NO DATA AT ALL")
        any_flags = True
        continue
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    flags = []

    # 1. zero/missing volume weeks (mid-history, not the very first partial week)
    if "Volume" in df.columns:
        zero_vol = df[(df["Volume"] == 0) | df["Volume"].isna()]
        zero_vol_mid = zero_vol[zero_vol.index > df.index[5]]   # skip inception noise
        if len(zero_vol_mid):
            flags.append(f"{len(zero_vol_mid)} zero/NaN-volume weeks "
                         f"(first: {zero_vol_mid.index[0].date()})")

    # 2. gaps between consecutive weekly bars, mid-history
    gaps = df.index.to_series().diff().dt.days
    big_gaps = gaps[(gaps > GAP_THRESHOLD_DAYS) & (gaps.index > df.index[5])]
    if len(big_gaps):
        worst = big_gaps.idxmax()
        flags.append(f"{len(big_gaps)} gaps > {GAP_THRESHOLD_DAYS}d (worst: {int(big_gaps.max())}d "
                     f"ending {worst.date()})")

    # 3. large single-week jumps not explained by a nearby split
    splits = yf.Ticker(inst.yf).splits
    split_dates = set(splits.index.date) if splits is not None and len(splits) else set()
    ret = df["Close"].pct_change()
    jumps = ret[ret.abs() > JUMP_THRESHOLD]
    unexplained = []
    for d, r in jumps.items():
        near_split = any(abs((d.date() - sd).days) <= 7 for sd in split_dates)
        if not near_split:
            unexplained.append((d, r))
    if unexplained:
        worst_d, worst_r = max(unexplained, key=lambda x: abs(x[1]))
        flags.append(f"{len(unexplained)} unexplained jumps >{JUMP_THRESHOLD:.0%} "
                     f"(worst: {worst_r*100:+.1f}% on {worst_d.date()})")

    if flags:
        any_flags = True
        print(f"{inst.key:<6} FLAGGED:")
        for f in flags:
            print(f"         - {f}")
    else:
        print(f"{inst.key:<6} clean ({len(df)} weekly bars, "
              f"{df.index[0].date()} to {df.index[-1].date()})")

print()
if not any_flags:
    print("No data-quality issues found across any of the 22 tickers.")
else:
    print("Some tickers flagged above -- worth a human look at the specific dates before "
          "trusting backtest results that lean heavily on those tickers/periods. A flag "
          "here doesn't necessarily mean bad data (real 25%+ weekly moves do happen, e.g. "
          "COVID-crash-era volatility) -- it means 'look before trusting'.")
