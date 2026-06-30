"""Test the critic's 'correlation-capped risk' proposal (#3): when avg pairwise correlation of the
18-ETF book exceeds a threshold, cut risk 0.5%->0.35%. Claim: 'DD will approach -35% in a 2008
event without this'. We test whether it IMPROVES the CAGR/|DD| ratio or is just another (already-
rejected) regime overlay that the trend filter makes redundant. Also (#1) quick check: do VIX
CLOSE>30 weeks have higher fwd returns than intraday-spike-only weeks?"""
import os
os.environ.setdefault("BROKER", "ib"); os.environ.setdefault("UNIVERSE", "etf")
import numpy as np, pandas as pd, yfinance as yf, sys
sys.path.insert(0, "D:/quant")
import dashboard.research.backtest as bt
from dashboard.instruments import ETF_UNIVERSE
from dashboard.research.backtest import ETF_CANDIDATES
from dashboard import core as _core

bt.CASH_YIELD = 0.043; _core.paper.WEEKLY_TREND_CLASSES = set()
uni = ETF_UNIVERSE + ETF_CANDIDATES
cands, closes = [], {}
for inst in uni:
    raw = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0: continue
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].copy(); df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None: df.index = df.index.tz_localize("UTC")
    if len(df) < 220: continue
    cands += bt._signals(df, inst.key); closes[inst.key] = df["close"]
span = (max(c["entry_date"] for c in cands) - min(c["entry_date"] for c in cands)).days/365.25

panel = pd.DataFrame(closes).sort_index()
rets = panel.pct_change()
W = 13
avgcorr = pd.Series(index=rets.index, dtype=float)
for i in range(W, len(rets)):
    sub = rets.iloc[i-W:i].dropna(axis=1, how="any")
    if sub.shape[1] >= 5:
        cm = sub.corr().values
        iu = np.triu_indices_from(cm, k=1)
        avgcorr.iloc[i] = np.nanmean(cm[iu])
avgcorr = avgcorr.shift(1)


def mets(regime):
    bt.REGIME = regime
    eq, real = bt._portfolio(cands, 0.005)
    m = bt._metrics(eq, real, span)
    sh = m["monthly_mean"]/m["monthly_std"]*np.sqrt(12) if m["monthly_std"] else 0
    return m["cagr"], m["maxdd"], sh


print(f"18-ETF book, {len(cands)} signals, {span:.1f}y | avg pairwise corr: "
      f"median {avgcorr.median():.2f}, 90th {avgcorr.quantile(.9):.2f}, max {avgcorr.max():.2f}")
cg0, dd0, sh0 = mets(None)
print(f"\n  {'config':<36}{'CAGR':>8}{'maxDD':>9}{'Sharpe':>8}{'CAGR/|DD|':>11}")
print(f"  {'baseline (no penalty)':<36}{cg0*100:>7.2f}%{dd0*100:>8.1f}%{sh0:>8.2f}{abs(cg0/dd0):>11.2f}")
for thr in [0.6, 0.7, 0.8]:
    reg = avgcorr.apply(lambda x: (0.35/0.5) if (np.isfinite(x) and x > thr) else 1.0)
    active = (avgcorr > thr).mean()
    cg, dd, sh = mets(reg)
    print(f"  {'corr>'+str(thr)+' -> 0.35% ('+f'{active*100:.0f}% wks)':<36}"
          f"{cg*100:>7.2f}%{dd*100:>8.1f}%{sh:>8.2f}{abs(cg/dd):>11.2f}")

bt.REGIME = None
print("\n(#1) VIX close>30 vs intraday-spike-only — fwd-4wk SPY return (weekly):")
vx = yf.download("^VIX", period="max", interval="1wk", progress=False, auto_adjust=True)
if hasattr(vx.columns, "nlevels") and vx.columns.nlevels > 1: vx.columns = vx.columns.get_level_values(0)
spyw = yf.download("SPY", period="max", interval="1wk", progress=False, auto_adjust=True)
if hasattr(spyw.columns, "nlevels") and spyw.columns.nlevels > 1: spyw.columns = spyw.columns.get_level_values(0)
sc = spyw["Close"]; fwd4 = sc.shift(-4)/sc - 1
vh, vc = vx["High"].reindex(sc.index), vx["Close"].reindex(sc.index)
for lbl, mask in [("VIX close>30", vc > 30), ("intraday>30 close<=30", (vh > 30) & (vc <= 30)),
                  ("calm (high<=30)", vh <= 30)]:
    r = fwd4[mask & fwd4.notna()]
    if len(r): print(f"  {lbl:<24} n={len(r):<4} fwd-4wk SPY mean {r.mean()*100:+.2f}% win {np.mean(r>0)*100:.0f}%")
