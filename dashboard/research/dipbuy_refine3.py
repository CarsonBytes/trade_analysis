"""Round-3 dip refinements from the 2026-06-29 critic: (1) relative-strength filter (only enter
when SPY underperforms RSP/VT over 5d => 'systemic panic'); (2) deep-overshoot size amplifier
(SPY far below 200MA); (3) VIX-crush early take-profit (exit when VIX drops >20%/2d while in
profit). Base signal = the ADOPTED spec: vix_panic + ADX>20. Core = 18-ETF book."""
import os
os.environ.setdefault("BROKER", "ib"); os.environ.setdefault("UNIVERSE", "etf")
import numpy as np, pandas as pd, yfinance as yf, sys
sys.path.insert(0, "D:/quant")
import dashboard.research.backtest as bt
from dashboard.instruments import ETF_UNIVERSE
from dashboard.research.backtest import ETF_CANDIDATES
from dashboard import core as _core

bt.CASH_YIELD = 0.043; _core.paper.WEEKLY_TREND_CLASSES = set()
cands = []
for inst in ETF_UNIVERSE + ETF_CANDIDATES:
    raw = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0: continue
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].copy(); df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None: df.index = df.index.tz_localize("UTC")
    if len(df) < 220: continue
    cands += bt._signals(df, inst.key)
eq_core, _ = bt._portfolio(cands, 0.005)
eq_core = eq_core[~eq_core.index.duplicated(keep="last")].sort_index()
cs, ce = eq_core.index[0], eq_core.index[-1]
didx = pd.date_range(cs, ce, freq="B", tz="UTC")
core_ret = eq_core.reindex(eq_core.index.union(didx)).ffill().reindex(didx).ffill().pct_change().fillna(0.0)
yrs = (didx[-1] - didx[0]).days / 365.25

# benchmarks for relative strength
bench = yf.download(["RSP", "VT"], period="max", interval="1d", progress=False, auto_adjust=True)["Close"]
rsp5 = bench["RSP"].pct_change(5); vt5 = bench["VT"].pct_change(5)


def dip(ticker, vix_exit=False):
    p = yf.download([ticker, "^VIX"], period="max", interval="1d", progress=False, auto_adjust=True)["Close"]
    s = p[ticker].dropna(); v = p["^VIX"].reindex(s.index).ffill()
    ph = yf.download(ticker, period="max", interval="1d", progress=False, auto_adjust=True)
    if hasattr(ph.columns, "nlevels") and ph.columns.nlevels > 1: ph.columns = ph.columns.get_level_values(0)
    h, lo = ph["High"].reindex(s.index), ph["Low"].reindex(s.index)
    ma5 = s.rolling(5).mean(); ma20 = s.rolling(20).mean(); ma200 = s.rolling(200).mean()
    d = s.diff(); g = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean(); r14 = 100 - 100/(1 + g/l.replace(0, np.nan))
    tr = pd.concat([(h-lo), (h-s.shift()).abs(), (lo-s.shift()).abs()], axis=1).max(axis=1)
    up = h.diff(); dn = -lo.diff(); pl = ((up > dn) & (up > 0))*up; mi = ((dn > up) & (dn > 0))*dn
    atr = tr.ewm(alpha=1/14, adjust=False).mean()
    pdi = 100*pl.ewm(alpha=1/14, adjust=False).mean()/atr; mdi = 100*mi.ewm(alpha=1/14, adjust=False).mean()/atr
    adx = (100*(pdi-mdi).abs()/(pdi+mdi).replace(0, np.nan)).ewm(alpha=1/14, adjust=False).mean()
    sv, vv, m5, m20, m200 = s.values, v.values, ma5.values, ma20.values, ma200.values
    r14v, adxv, idx = r14.values, adx.values, s.index
    rs_sp = (s.pct_change(5) - rsp5.reindex(s.index)).values
    rs_vt = (s.pct_change(5) - vt5.reindex(s.index)).values
    ent = (sv < m20*0.975) & (vv/np.roll(vv, 5)-1 > 0.15) & (r14v < 35) & (adxv > 20)
    ent = np.nan_to_num(ent).astype(bool)
    n = len(sv); COST = 0.0010; out = []; i = 200
    while i < n-1:
        if not ent[i]: i += 1; continue
        e = sv[i]; j = i+1; R = None
        while j < n:
            r = sv[j]/e - 1.0
            if vix_exit and r > 0 and (vv[j]/vv[max(j-2, 0)]-1) < -0.20: R = r; break
            if sv[j] >= m5[j] or r >= 0.03: R = r; break
            if r <= -0.05: R = -0.05; break
            if (j-i) >= 10: R = r; break
            j += 1
        if R is None: R = sv[min(j, n-1)]/e - 1.0
        out.append({"d": idx[min(j, n-1)], "r": R-COST, "vix": float(vv[i]),
                    "over": float(sv[i]/m200[i]-1) if np.isfinite(m200[i]) else np.nan,
                    "rs_sp": float(rs_sp[i]) if np.isfinite(rs_sp[i]) else np.nan,
                    "rs_vt": float(rs_vt[i]) if np.isfinite(rs_vt[i]) else np.nan})
        i = j+1
    return out


def unit(trs):
    u = pd.Series(0.0, index=didx)
    for t in trs:
        dt = pd.Timestamp(t["d"]); dt = dt.tz_localize("UTC") if dt.tz is None else dt
        if cs <= dt <= ce: u.iloc[didx.searchsorted(dt)] += t["r"]
    return u


def met(ret):
    eqc = (1+ret).cumprod(); mo = eqc.resample("ME").last().pct_change().dropna()
    return (eqc.iloc[-1]**(1/yrs)-1, (eqc/eqc.cummax()-1).min(),
            mo.mean()/mo.std()*np.sqrt(12) if mo.std() > 0 else 0)


base = dip("SPY")
rb = np.array([t["r"] for t in base])
c0 = met(core_ret)
print(f"core {c0[0]*100:+.2f}%/{c0[1]*100:.1f}%/Sh{c0[2]:.2f} | base dip (vix_panic+ADX>20): "
      f"n={len(rb)} meanR {rb.mean()*100:+.2f}% win {np.mean(rb>0)*100:.0f}%\n")

print("(1) RELATIVE-STRENGTH at entry (does SPY-underperform-RSP/VT pick stronger bounces?):")
db = pd.DataFrame(base)
for col, lbl in [("rs_sp", "vs RSP"), ("rs_vt", "vs VT")]:
    sub = db.dropna(subset=[col])
    under = sub[sub[col] < -0.01]["r"].values     # SPY underperformed bench by >1% (critic's filter)
    over = sub[sub[col] >= -0.01]["r"].values
    if len(under) and len(over):
        print(f"  {lbl}: SPY underperf>1% n={len(under)} meanR {under.mean()*100:+.2f}% | "
              f"else n={len(over)} meanR {over.mean()*100:+.2f}%  (filter helps only if LEFT>RIGHT & worth the n-loss)")

print("\n(2) DEEP-OVERSHOOT buckets (SPY vs 200MA at entry) — adds beyond VIX, or redundant?):")
for lo, hi, lbl in [(-99, -.10, "<-10%"), (-.10, -.05, "-10..-5%"), (-.05, 0, "-5..0%"), (0, 99, ">0% (above 200MA)")]:
    sub = db[(db["over"] >= lo) & (db["over"] < hi)]["r"].values
    if len(sub): print(f"  overshoot {lbl:<18} n={len(sub):<3} meanR {sub.mean()*100:+.2f}% win {np.mean(sub>0)*100:.0f}%")

print("\n(3) VIX-CRUSH early take-profit (exit if VIX -20%/2d while in profit) vs base:")
vx = dip("SPY", vix_exit=True); rv = np.array([t["r"] for t in vx])
print(f"  base       : n={len(rb)} meanR {rb.mean()*100:+.2f}% win {np.mean(rb>0)*100:.0f}%")
print(f"  +VIX-crush : n={len(rv)} meanR {rv.mean()*100:+.2f}% win {np.mean(rv>0)*100:.0f}%")
# blend impact (SPY+QQQ+XLK) base vs +vix-crush, risk-matched 0.10
pb = [t for tk in ["SPY", "QQQ", "XLK"] for t in dip(tk)]
pv = [t for tk in ["SPY", "QQQ", "XLK"] for t in dip(tk, vix_exit=True)]
b = met(core_ret + 0.10*unit(pb)); w = met(core_ret + 0.10*unit(pv))
print(f"  blend base       : {b[0]*100:+.2f}%/{b[1]*100:.1f}%/Sh{b[2]:.2f}")
print(f"  blend +VIX-crush : {w[0]*100:+.2f}%/{w[1]*100:.1f}%/Sh{w[2]:.2f}")
