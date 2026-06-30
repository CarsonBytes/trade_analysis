"""Test the 2026-06-29 critics' core/ops proposals: (A) vol-targeting @12%/15% (claimed DD
-9.6->-8.2, Sharpe ->1.15 -- VERIFY, prior runs say it's a leverage dial); (B) monthly- vs
weekly-bar signals (the 'rebalance less' idea); (C) VIX-scaled contributions (DCA more at VIX>30)
via conditional forward returns. Core = 18-ETF screened book, cash@4.3%."""
import os
os.environ.setdefault("BROKER", "ib"); os.environ.setdefault("UNIVERSE", "etf")
import numpy as np, pandas as pd, yfinance as yf, sys
sys.path.insert(0, "D:/quant")
import dashboard.research.backtest as bt
from dashboard.instruments import ETF_UNIVERSE
from dashboard.research.backtest import ETF_CANDIDATES
from dashboard import core as _core

bt.CASH_YIELD = 0.043; _core.paper.WEEKLY_TREND_CLASSES = set()


def build(interval):
    cands = []
    for inst in ETF_UNIVERSE + ETF_CANDIDATES:
        raw = yf.download(inst.yf, period="max", interval=interval, progress=False, auto_adjust=True)
        if raw is None or len(raw) == 0: continue
        if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
            raw.columns = raw.columns.get_level_values(0)
        df = raw[["Open", "High", "Low", "Close"]].copy(); df.columns = ["open", "high", "low", "close"]
        df = df.dropna()
        if df.index.tz is None: df.index = df.index.tz_localize("UTC")
        if len(df) < (220 if interval == "1wk" else 60): continue
        cands += bt._signals(df, inst.key)
    return cands


def metrics(eq, realized, years):
    m = bt._metrics(eq, realized, years)
    return m["cagr"], m["maxdd"], (m["monthly_mean"]/m["monthly_std"]*np.sqrt(12) if m["monthly_std"] else 0)


cw = build("1wk")
spanw = (max(c["entry_date"] for c in cw) - min(c["entry_date"] for c in cw)).days/365.25

print("(A) VOL-TARGETING vs fixed 0.5% (critic claims DD->-8.2%, Sharpe->1.15):")
eqf, rf = bt._portfolio(cw, 0.005)
cg, dd, sh = metrics(eqf, rf, spanw)
print(f"  fixed 0.5%        : CAGR {cg*100:+.2f}% | maxDD {dd*100:.1f}% | Sharpe {sh:.2f} | n={len(rf)}")
for tv in [0.12, 0.15]:
    eqv, rv = bt._portfolio(cw, 0.005, target_vol=tv, tpy=len(rf)/spanw)
    cg2, dd2, sh2 = metrics(eqv, rv, spanw)
    print(f"  voltarget {tv*100:.0f}%/yr : CAGR {cg2*100:+.2f}% | maxDD {dd2*100:.1f}% | Sharpe {sh2:.2f} "
          f"| CAGR/|DD| {abs(cg2/dd2):.2f} (fixed {abs(cg/dd):.2f})")
print("  => judge: does it IMPROVE the CAGR/|DD| ratio, or just rescale CAGR & DD together (=leverage)?\n")

print("(B) MONTHLY vs WEEKLY signal bars (the 'rebalance monthly' idea):")
cm = build("1mo")
spanm = (max(c["entry_date"] for c in cm) - min(c["entry_date"] for c in cm)).days/365.25
eqm, rm = bt._portfolio(cm, 0.005)
cgm, ddm, shm = metrics(eqm, rm, spanm)
print(f"  weekly bars : CAGR {cg*100:+.2f}% | maxDD {dd*100:.1f}% | Sharpe {sh:.2f} | "
      f"{len(rf)} trades (~{len(rf)/spanw:.0f}/yr)")
print(f"  monthly bars: CAGR {cgm*100:+.2f}% | maxDD {ddm*100:.1f}% | Sharpe {shm:.2f} | "
      f"{len(rm)} trades (~{len(rm)/spanm:.0f}/yr)  [NB monthly bars => ~5-MONTH hold, a slower system]")
print("  (the live strategy is SIGNAL-driven ~32 rt/yr, NOT a 52x periodic rebalance — costs already in R)\n")

print("(C) VIX-scaled contributions: forward returns when VIX>30 vs always (does buying the panic pay?):")
sv = yf.download(["SPY", "^VIX"], period="max", interval="1mo", progress=False, auto_adjust=True)["Close"].dropna()
spy, vix = sv["SPY"], sv["^VIX"]
fwd12 = spy.shift(-12) / spy - 1.0
for lbl, mask in [("ALL months", pd.Series(True, index=spy.index)),
                  ("VIX>25 month", vix > 25), ("VIX>30 month", vix > 30)]:
    r = fwd12[mask & fwd12.notna()]
    if len(r):
        print(f"  contribute in {lbl:<14} n={len(r):<4} mean fwd-12mo SPY {r.mean()*100:+.1f}% | "
              f"median {r.median()*100:+.1f}% | win {np.mean(r>0)*100:.0f}%")
print("  => if VIX>30 months have HIGHER forward returns, front-loading contributions there is +EV (DCA timing).")
