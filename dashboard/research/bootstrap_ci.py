"""Block-bootstrap confidence interval for Calmar/Sharpe -- every figure documented in this
project so far is a single point estimate (e.g. "Calmar 0.854"). This answers "how uncertain
is that number" instead.

Method: a MOVING-BLOCK bootstrap by CALENDAR YEAR (not a naive i.i.d. per-trade resample,
which would shred the autocorrelation trend-following trades actually have -- a 2008-style
year's trades are correlated with each other, not independent draws). Each bootstrap sample
draws N_YEARS calendar-years WITH replacement from the real history, keeps each drawn year's
trades in their original internal order, and re-sequences them into a synthetic back-to-back
timeline so _portfolio()'s chronological-walk assumptions (sorted, monotonic entry_date) still
hold. Runs the REAL _portfolio()/_metrics() pipeline on each resampled timeline -- not a
simplified R-multiple-only approximation -- so position sizing, one-per-instrument
de-correlation, and POS_CAP/PORTFOLIO_CAP all apply exactly as they do in the real backtest.

Run:  uv run python -m dashboard.research.bootstrap_ci [n_draws]
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import sys
import random
import numpy as np
import pandas as pd
import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe

N_DRAWS = int(sys.argv[1]) if len(sys.argv) > 1 else 500

bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0
bt.CASH_YIELD = None            # a resampled/shuffled timeline has no real calendar dates to
                                 # look up a real cash-yield series against -- strategy-only

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
by_year: dict[int, list] = {}
for c in cands:
    by_year.setdefault(c["entry_date"].year, []).append(c)
years = sorted(by_year)
n_years = len(years)
print(f"{len(cands)} signals across {n_years} calendar years "
      f"({years[0]}-{years[-1]})\n")

# point estimate on the REAL (unshuffled) history, for reference
eq0, real0 = bt._portfolio(cands, 0.01)
yrs0 = (cands[-1]["entry_date"] - cands[0]["entry_date"]).days / 365.25
m0 = bt._metrics(eq0, real0, yrs0)
calmar0 = m0["cagr"] / abs(m0["maxdd"]) if m0["maxdd"] else 0
print(f"POINT ESTIMATE (real, unshuffled history): CAGR {m0['cagr']*100:.2f}%  "
      f"maxDD {m0['maxdd']*100:.2f}%  Calmar {calmar0:.3f}\n")


def _resample_once() -> tuple[float, float, float]:
    """Draw n_years calendar-years WITH replacement, re-sequence into a synthetic
    back-to-back monotonic timeline, run the real portfolio pipeline."""
    drawn = random.choices(years, k=n_years)
    synth = []
    cursor = pd.Timestamp("2000-01-01", tz="UTC")
    for y in drawn:
        block = by_year[y]
        block_start = min(c["entry_date"] for c in block)
        offset = cursor - block_start
        for c in block:
            c2 = dict(c)
            c2["entry_date"] = c["entry_date"] + offset
            c2["exit_date"] = c["exit_date"] + offset
            synth.append(c2)
        block_end = max(c["exit_date"] for c in block)
        cursor = block_end + offset + pd.Timedelta(days=1)
    synth.sort(key=lambda c: c["entry_date"])
    eq, real = bt._portfolio(synth, 0.01)
    if len(real) < 5:
        return None
    yrs = max((synth[-1]["entry_date"] - synth[0]["entry_date"]).days / 365.25, 0.1)
    m = bt._metrics(eq, real, yrs)
    calmar = m["cagr"] / abs(m["maxdd"]) if m["maxdd"] else 0
    return m["cagr"], m["maxdd"], calmar


print(f"Running {N_DRAWS} block-bootstrap draws (this re-runs the full portfolio "
      f"simulation each time)...")
cagrs, dds, calmars = [], [], []
random.seed(42)               # reproducible
for i in range(N_DRAWS):
    r = _resample_once()
    if r is None:
        continue
    cagrs.append(r[0]); dds.append(r[1]); calmars.append(r[2])
    if (i + 1) % 100 == 0:
        print(f"  {i+1}/{N_DRAWS}...")

cagrs, dds, calmars = np.array(cagrs), np.array(dds), np.array(calmars)
print(f"\n{len(cagrs)} valid draws (some early skipped for too few trades in a resample).\n")


def _pct(arr, p):
    return np.percentile(arr, p)


print("BLOCK-BOOTSTRAP DISTRIBUTION (year-level resampling, 500 draws):")
print(f"  CAGR:    median {_pct(cagrs,50)*100:+.2f}%   "
      f"90% CI [{_pct(cagrs,5)*100:+.2f}%, {_pct(cagrs,95)*100:+.2f}%]")
print(f"  maxDD:   median {_pct(dds,50)*100:.2f}%   "
      f"90% CI [{_pct(dds,5)*100:.2f}%, {_pct(dds,95)*100:.2f}%]")
print(f"  Calmar:  median {_pct(calmars,50):.3f}   "
      f"90% CI [{_pct(calmars,5):.3f}, {_pct(calmars,95):.3f}]")
print(f"\n  P(Calmar < 0) = {(calmars < 0).mean():.1%}   "
      f"P(Calmar < 0.5) = {(calmars < 0.5).mean():.1%}")
print("\nNOTE: this resamples WHICH YEARS occur and in what order, not what happens within a "
      "year -- so it captures 'what if the mix/sequence of regimes had been different' "
      "uncertainty, not intra-year path uncertainty. A single unlucky draw (e.g. several "
      "2001-2006-like years back to back) is exactly the scenario this is meant to quantify.")
