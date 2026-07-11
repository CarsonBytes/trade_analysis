"""Every sleeve backtest so far tests a STATIC ticker universe (3-only or the full 11) --
nobody has backtested the actual STAGED TRANSITION the account will now really experience:
SLEEVE_STAGE_2A (SPY/QQQ/XLK) for the first 3 months, +DIA/IWM for months 3-6, then the full
11 onward (core/sleeve.py's SLEEVE_STAGE_2B_MONTHS=3.0 / SLEEVE_STAGE_2C_MONTHS=6.0). This
checks, across MANY historical 6-month windows (not just one arbitrarily-picked start date),
how much the staged ramp's first 6 months typically differs from jumping straight to the full
11-ticker book -- the honest "what to expect in months 1-6" answer, not the steady-state figure
that's been quoted so far.

Run:  uv run python -m dashboard.research.sleeve_staged_ramp
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import numpy as np
import pandas as pd

from dashboard.core.sleeve import (SLEEVE_UNIVERSE, SLEEVE_STAGE_2A, SLEEVE_STAGE_2B_ADD,
                                    SLEEVE_STAGE_2C_ADD)
from dashboard.research.sleeve_blend import _sleeve_trades, _sleeve_unit_series

STAGE_5 = SLEEVE_STAGE_2A + SLEEVE_STAGE_2B_ADD    # 3+2 = 5 tickers, months 3-6

print(f"Fetching sleeve data for {len(SLEEVE_UNIVERSE)} tickers...")
sleeve_trades = {tk: _sleeve_trades(tk) for tk in SLEEVE_UNIVERSE}

# build a shared daily index spanning the full sleeve history
all_dates = sorted({pd.Timestamp(t["d"]).tz_localize("UTC") if pd.Timestamp(t["d"]).tz is None
                    else pd.Timestamp(t["d"])
                    for trs in sleeve_trades.values() for t in trs})
didx = pd.date_range(all_dates[0], all_dates[-1], freq="B", tz="UTC")

unit_2a = sum((_sleeve_unit_series(sleeve_trades[tk], didx) for tk in SLEEVE_STAGE_2A),
             pd.Series(0.0, index=didx))
unit_5 = sum((_sleeve_unit_series(sleeve_trades[tk], didx) for tk in STAGE_5),
            pd.Series(0.0, index=didx))
unit_11 = sum((_sleeve_unit_series(sleeve_trades[tk], didx) for tk in SLEEVE_UNIVERSE),
             pd.Series(0.0, index=didx))

# candidate start dates: the 1st of every quarter across the usable history (skip the first
# few years so there's always a full 6mo window of real data ahead)
start = didx[0] + pd.Timedelta(days=730)
end_cutoff = didx[-1] - pd.Timedelta(days=185)
starts = pd.date_range(start, end_cutoff, freq="QS", tz="UTC")

print(f"Testing {len(starts)} candidate 6-month ramp windows "
      f"({starts[0].date()} to {starts[-1].date()})...\n")

staged_totals, full_totals, diffs = [], [], []
for s in starts:
    m3 = s + pd.Timedelta(days=91)
    m6 = s + pd.Timedelta(days=182)
    # staged: 2A contribution for months 0-3, STAGE_5 contribution for months 3-6
    staged = unit_2a[(unit_2a.index >= s) & (unit_2a.index < m3)].sum() + \
             unit_5[(unit_5.index >= m3) & (unit_5.index < m6)].sum()
    full = unit_11[(unit_11.index >= s) & (unit_11.index < m6)].sum()
    staged_totals.append(staged)
    full_totals.append(full)
    diffs.append(staged - full)

staged_totals = np.array(staged_totals)
full_totals = np.array(full_totals)
diffs = np.array(diffs)

print(f"{'start':<12}{'staged R':>10}{'full-11 R':>11}{'diff':>9}")
for s, st, fl, d in list(zip(starts, staged_totals, full_totals, diffs))[:10]:
    print(f"{s.date()!s:<12}{st*100:>9.1f}%{fl*100:>10.1f}%{d*100:>8.1f}%")
print("  ...")

print(f"\nAcross all {len(starts)} windows (6-month combined sleeve R contribution, unweighted):")
print(f"  staged (2A->5, real ramp):  mean {staged_totals.mean()*100:+.2f}%  "
      f"median {np.median(staged_totals)*100:+.2f}%  std {staged_totals.std()*100:.2f}pp")
print(f"  full-11 (counterfactual):   mean {full_totals.mean()*100:+.2f}%  "
      f"median {np.median(full_totals)*100:+.2f}%  std {full_totals.std()*100:.2f}pp")
print(f"  difference (staged - full): mean {diffs.mean()*100:+.2f}pp  "
      f"median {np.median(diffs)*100:+.2f}pp")
print(f"  staged BEAT full-11 in {(diffs > 0).mean()*100:.0f}% of windows")
print("\nNOTE: this is RAW combined R contribution over the 6-month window (not annualized "
      "Calmar/Sharpe -- 6 months is too short a window for those to be meaningful on their "
      "own). Interpret as 'how much sleeve return did the staged ramp capture vs the full "
      "book, historically, across many different possible starting regimes' -- not a precise "
      "point forecast for THIS specific 6-month period starting now.")
