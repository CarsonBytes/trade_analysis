"""Cash-rate sensitivity curve -- ADDED 2026-08-06, user-requested critique verification
(critic 1, item #2). README already states a single breakeven point (core-only beats a
risk-matched SPY+cash alternative only above ~5.5% cash rate) -- this turns that one point
into a proper curve, using the EXACT SAME methodology bootstrap_ci.py uses for the project's
own headline "CAGR 6.06%, Calmar 0.887" figure (bt.POS_CAP=0.25, PORTFOLIO_CAP=1.0, risk=1%,
core+reentry-gate signals, same trade-count-weighted 30% NRA dividend-withholding drag) so
these numbers are directly comparable to that documented baseline, not a separately-invented
methodology.

3 rate scenarios:
  1. Constant 0% (a genuine ZIRP floor)
  2. Constant 2% (a plausible post-cut landing rate)
  3. Real 2000-2015 ^IRX weekly series, tiled across the full ~30y backtest span (preserves
     the REAL shape/volatility of that specific low-rate era rather than just its average)
plus the current constant 4.3% for direct comparison to the documented headline figure.

Run: uv run python -u -m dashboard.research.rate_sensitivity_test
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import pandas as pd
import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe

WITHHOLD_RATE = 0.30
bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0
RISK = 0.01

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
counts: dict[str, int] = {}
for c in cands:
    counts[c["key"]] = counts.get(c["key"], 0) + 1
total_n = sum(counts.values())
blended_yield = sum(counts.get(k, 0) / total_n * yields.get(k, 0.0) for k in yields)
DRAG_PCT = WITHHOLD_RATE * blended_yield
print(f"{len(cands)} signals. Trade-count-weighted blended yield {blended_yield*100:.2f}% "
     f"-> dividend withholding drag -{DRAG_PCT*100:.2f}pp/yr CAGR (applied to every scenario)\n")

years = (cands[-1]["entry_date"] - cands[0]["entry_date"]).days / 365.25


def _run(label: str, cash_yield) -> None:
    bt.CASH_YIELD = cash_yield
    eq, real = bt._portfolio(cands, RISK)
    m = bt._metrics(eq, real, years)
    cagr_at = m["cagr"] - DRAG_PCT
    calmar = cagr_at / abs(m["maxdd"]) if m["maxdd"] else 0
    print(f"  {label:<45} CAGR {cagr_at*100:+7.2f}%  maxDD {m['maxdd']*100:7.2f}%  "
         f"Calmar {calmar:6.3f}")


print("RATE SENSITIVITY (core-only, after-tax, same methodology as the documented "
     "CAGR 6.06%/Calmar 0.887 headline figure):\n")

_run("Current constant 4.3% (matches documented headline)", 0.043)
_run("Constant 0% (ZIRP floor)", 0.0)
_run("Constant 2% (plausible post-cut landing rate)", 0.02)

print("\nFetching real ^IRX 2000-2015 weekly series (a genuine sustained low-rate era, "
     "incl. the 2000-01 peak AND the 2008-15 ZIRP trough)...")
irx = yf.download("^IRX", period="max", interval="1wk", progress=False, auto_adjust=True)
if hasattr(irx.columns, "nlevels") and irx.columns.nlevels > 1:
    irx.columns = irx.columns.get_level_values(0)
irx_s = (irx["Close"].dropna() / 100.0)
if irx_s.index.tz is None:
    irx_s.index = irx_s.index.tz_localize("UTC")
slice_00_15 = irx_s[(irx_s.index >= "2000-01-01") & (irx_s.index <= "2015-12-31")]
print(f"  real 2000-2015 slice: {len(slice_00_15)} weeks, mean {slice_00_15.mean()*100:.2f}%, "
     f"range [{slice_00_15.min()*100:.2f}%, {slice_00_15.max()*100:.2f}%]")

# Tile the REAL 15y rate path (values only, preserving its shape) across the full backtest
# date range -- repeats the pattern end-to-end rather than collapsing it to a single average,
# so week-to-week rate volatility within that era is preserved, not just its level.
bt_start, bt_end = cands[0]["entry_date"], cands[-1]["exit_date"]
full_idx = pd.date_range(bt_start, bt_end, freq="W-FRI", tz="UTC")
tile_vals = slice_00_15.values
tiled = pd.Series([tile_vals[i % len(tile_vals)] for i in range(len(full_idx))], index=full_idx)
_run("Real 2000-2015 ^IRX pattern, tiled across full span", tiled)

print("\nNOTE: dividend-withholding drag (DRAG_PCT) is a fixed constant across all scenarios "
     "-- only the cash-yield component varies, isolating the rate-cycle effect the critique "
     "asked about from the (unrelated) tax-drag question already answered elsewhere.")
