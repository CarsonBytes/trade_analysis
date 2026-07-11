"""Estimate the CAGR drag from US dividend withholding tax that the backtest currently
ignores. yfinance's auto_adjust=True (used everywhere in this project's backtests) folds
every dividend back into the price as if it were reinvested tax-free -- but a real HK NRA
account has 30% withheld on US-source dividends before they land. Flagged once before
(HANDOFF, re: the sleeve's short ~3wk holds: "weak for a 3wk-hold book, matters on bond
sleeves only") but never quantified for the CORE book's cumulative, repeated exposure to
several meaningfully-yielding bond/preferred ETFs (TLT/IEF/SHY/HYG/TIP/PFF/HYD/CWB).

Method (an ESTIMATE, not a bar-by-bar price reconstruction): pull each ticker's trailing
12-month dividend yield, weight by that ticker's share of REALIZED trades in the correctly-
scoped full-history backtest (a reasonable proxy for time-weighted portfolio exposure, since
position sizing is risk-based and roughly uniform per trade), compute the blended yield, and
apply -0.30 x blended_yield as a constant CAGR drag. Transparent and order-of-magnitude
correct; not a precise re-run of the whole backtest at "true" after-tax prices.

Run:  uv run python -m dashboard.research.dividend_tax_drag
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

WITHHOLD_RATE = 0.30

print(f"Fetching full-history weekly data + trailing dividend yields "
      f"({len(active_universe())} instruments)...")
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

print("\nTrailing-12mo dividend yield by ticker:")
for k, y in sorted(yields.items(), key=lambda kv: -kv[1]):
    print(f"  {k:<6} {y*100:5.2f}%")

# weight by each ticker's share of REALIZED trades (proxy for time-weighted exposure)
cands = sorted(cands, key=lambda c: c["entry_date"])  # _portfolio() sorts internally too,
                                                       # but yrs below needs it pre-sorted --
                                                       # cands[0]/cands[-1] on an UNSORTED list
                                                       # picks up whichever instrument happened
                                                       # to iterate first/last, not the true
                                                       # chronological span (caught this exact
                                                       # bug the first time this script ran:
                                                       # CAGR came out 8.57% instead of the
                                                       # correct ~4.7%, from an understated yrs
                                                       # denominator)
counts: dict[str, int] = {}
for c in cands:
    counts[c["key"]] = counts.get(c["key"], 0) + 1
total_n = sum(counts.values())
blended_yield = sum(counts.get(k, 0) / total_n * yields.get(k, 0.0) for k in yields)
print(f"\n{total_n} total signals across {len(counts)} instruments")
print(f"Trade-count-weighted blended portfolio yield: {blended_yield*100:.2f}%")

drag_pct = WITHHOLD_RATE * blended_yield
print(f"Estimated 30% withholding tax drag: -{drag_pct*100:.2f}pp/yr on CAGR "
      f"(0.30 x {blended_yield*100:.2f}%)")

eq, realized = bt._portfolio(cands, 0.01)
yrs = (cands[-1]["entry_date"] - cands[0]["entry_date"]).days / 365.25
m = bt._metrics(eq, realized, yrs)
calmar_before = m["cagr"] / abs(m["maxdd"]) if m["maxdd"] else 0
cagr_after = m["cagr"] - drag_pct
calmar_after = cagr_after / abs(m["maxdd"]) if m["maxdd"] else 0
print(f"\nCurrent config, strategy-only, no cash-yield (1% risk):")
print(f"  BEFORE dividend-tax adjustment: CAGR {m['cagr']*100:.2f}%  maxDD {m['maxdd']*100:.2f}%  "
      f"Calmar {calmar_before:.3f}")
print(f"  AFTER  dividend-tax adjustment: CAGR {cagr_after*100:.2f}%  maxDD {m['maxdd']*100:.2f}%  "
      f"Calmar {calmar_after:.3f}  (maxDD unchanged -- a steady yield drag barely moves the "
      f"WORST drawdown, it mainly lowers the compounding rate)")
print("\nNOTE: this is an ESTIMATE (trade-count-weighted average yield x 30%, applied as a "
      "constant annual drag), not a bar-by-bar reconstruction of after-tax adjusted prices. "
      "Real drag varies by which specific tickers are held and for how long in any given "
      "year -- treat this as 'roughly how big', not a precise number.")
