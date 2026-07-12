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

FIXED 2026-07-12: the original version hardcoded `bt.CASH_YIELD = None` ("strategy-only") with
a real reason -- a resampled/reindexed synthetic timeline has no genuine calendar dates to look
up the real ^IRX T-bill series against. But that meant this CI was never actually computed
against the current best-validated, cash-yield-ON headline config (Calmar 0.943, cross-validated
in sleeve_blend.py -- see HANDOFF) -- it's a DIFFERENT, more conservative baseline (point estimate
0.588) that doesn't cross-apply to 0.943, a distinction a pasted external critique conflated by
computing "0.943 -> 0.488 = -48%" as if those were the same quantity's before/after (they are two
different scripts' outputs; the correct within-methodology tax-drag comparison is 0.588->0.488,
-17% relative -- see dividend_tax_drag.py). Fixed here by using a CONSTANT rate instead of the
real ^IRX series: `_rate()` in backtest.py returns a constant unconditionally regardless of
`asof`, so it's immune to the resampling/reindexing problem entirely. Also folds in the SAME
trade-count-weighted dividend withholding tax drag as dividend_tax_drag.py, applied per-draw --
so this now answers "what's the uncertainty band on the actual after-tax, cash-yield-inclusive
number a real HK NRA account would see", not an internally-inconsistent strategy-only proxy for it.

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
WITHHOLD_RATE = 0.30            # same 30% US NRA dividend withholding as dividend_tax_drag.py

bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0
bt.CASH_YIELD = 0.043            # CONSTANT rate (today's IB USD cash yield, matches the
                                  # --cash-rate convention elsewhere in backtest.py) instead of
                                  # the real ^IRX series -- a constant is immune to the
                                  # resampling/reindexing problem a real dated series has (see
                                  # the FIXED note above), so this bootstrap now includes cash-
                                  # yield's smoothing effect instead of excluding it entirely.

print(f"Fetching full-history weekly data ({len(active_universe())} instruments)...")
cands = []
yields: dict[str, float] = {}
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

    # SAME trailing-12mo yield lookup as dividend_tax_drag.py, so the tax-drag figure folded
    # into this bootstrap is computed identically, not a second, possibly-diverging estimate.
    t = yf.Ticker(inst.yf)
    div = t.dividends
    if div is not None and len(div):
        cutoff = div.index[-1] - pd.Timedelta(days=365)
        trailing_div = div[div.index >= cutoff].sum()
        last_px = float(df["close"].iloc[-1])
        yields[inst.key] = (trailing_div / last_px) if last_px else 0.0
    else:
        yields[inst.key] = 0.0

cands = sorted(cands, key=lambda c: c["entry_date"])
by_year: dict[int, list] = {}
for c in cands:
    by_year.setdefault(c["entry_date"].year, []).append(c)
years = sorted(by_year)
n_years = len(years)
print(f"{len(cands)} signals across {n_years} calendar years "
      f"({years[0]}-{years[-1]})\n")

# same trade-count-weighted blended yield as dividend_tax_drag.py -- a single constant drag
# (in CAGR percentage points), computed ONCE on the real (unresampled) trade distribution and
# applied identically to every bootstrap draw below (drawing different YEARS doesn't change
# which tickers exist or their trailing yield, only how often each one's trades recur).
counts: dict[str, int] = {}
for c in cands:
    counts[c["key"]] = counts.get(c["key"], 0) + 1
total_n = sum(counts.values())
blended_yield = sum(counts.get(k, 0) / total_n * yields.get(k, 0.0) for k in yields)
DRAG_PCT = WITHHOLD_RATE * blended_yield
print(f"Trade-count-weighted blended portfolio yield: {blended_yield*100:.2f}%  "
      f"-> dividend withholding drag: -{DRAG_PCT*100:.2f}pp/yr CAGR (applied to every draw)\n")

# point estimate on the REAL (unshuffled) history, for reference
eq0, real0 = bt._portfolio(cands, 0.01)
yrs0 = (cands[-1]["entry_date"] - cands[0]["entry_date"]).days / 365.25
m0 = bt._metrics(eq0, real0, yrs0)
cagr0_at = m0["cagr"] - DRAG_PCT
calmar0 = cagr0_at / abs(m0["maxdd"]) if m0["maxdd"] else 0
print(f"POINT ESTIMATE (real, unshuffled history, cash-yield {bt.CASH_YIELD:.1%} + after-tax): "
      f"CAGR {cagr0_at*100:.2f}%  maxDD {m0['maxdd']*100:.2f}%  Calmar {calmar0:.3f}\n")


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
    cagr_at = m["cagr"] - DRAG_PCT             # after-tax CAGR (same constant drag every draw)
    calmar = cagr_at / abs(m["maxdd"]) if m["maxdd"] else 0
    return cagr_at, m["maxdd"], calmar


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


print(f"BLOCK-BOOTSTRAP DISTRIBUTION (year-level resampling, {N_DRAWS} draws, "
      f"cash-yield {bt.CASH_YIELD:.1%} + after-tax):")
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
