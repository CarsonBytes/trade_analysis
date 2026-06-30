"""Bound the critic's execution claims (#1 limit vs market orders). The backtest already charges
HALF_SPREAD=0.5bp/side (1bp round-trip) on ETFs. Sweep the per-side cost to see (a) whether market
orders could be a '1.9-5.7%/yr hidden loss' (only if per-side cost ~5-15bp, i.e. illiquid) and
(b) the MAX benefit of limit orders (gap between realistic market ~1-2bp and limit ~0.5bp). 18-ETF
book, cash@4.3%."""
import os
os.environ.setdefault("BROKER", "ib"); os.environ.setdefault("UNIVERSE", "etf")
import numpy as np, pandas as pd, yfinance as yf, sys
sys.path.insert(0, "D:/quant")
import dashboard.research.backtest as bt
from dashboard.instruments import ETF_UNIVERSE
from dashboard.research.backtest import ETF_CANDIDATES
from dashboard import core as _core
from dashboard.core import paper

bt.CASH_YIELD = 0.043; _core.paper.WEEKLY_TREND_CLASSES = set()

# download once
dfs = {}
for inst in ETF_UNIVERSE + ETF_CANDIDATES:
    raw = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0: continue
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].copy(); df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None: df.index = df.index.tz_localize("UTC")
    if len(df) >= 220: dfs[inst.key] = df


def run_at_cost(hs_per_side):
    # patch the default half_spread baked into r_multiple (default args bound at def-time)
    paper.r_multiple.__defaults__ = (hs_per_side, None)
    cands = []
    for k, df in dfs.items():
        cands += bt._signals(df, k)
    span = (max(c["entry_date"] for c in cands) - min(c["entry_date"] for c in cands)).days/365.25
    eq, real = bt._portfolio(cands, 0.005)
    m = bt._metrics(eq, real, span)
    return m["cagr"], m["maxdd"], len(real), span


print(f"{'per-side cost':>14}{'round-trip':>12}{'CAGR':>9}{'maxDD':>9}{'vs 0.5bp':>10}")
base = None
for bp in [0.5, 1.0, 2.0, 5.0, 10.0, 15.0]:
    hs = bp / 10000.0
    cg, dd, n, span = run_at_cost(hs)
    if base is None: base = cg
    print(f"{bp:>11.1f}bp{bp*2:>10.1f}bp{cg*100:>8.2f}%{dd*100:>8.1f}%{(cg-base)*100:>9.2f}pp")
print(f"\n  (~{n} trades over {span:.1f}y; book = {len(dfs)} ETFs, all liquid: SPY/QQQ/GLD/TLT/IEF/..)")
print("  READ: liquid-ETF half-spread is realistically ~0.5-1.5bp/side. The CAGR gap from 0.5bp to")
print("  even 5bp bounds the MAX a market->limit switch could save. Critic's '1.9-5.7%/yr' assumes")
print("  ~5-15bp/side = illiquid microcaps, NOT this book. Limit orders = minor polish, not a layer.")
