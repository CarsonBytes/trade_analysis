"""Round-2 dip refinements from the 2026-06-29 critics: (1) RSI(2)<10-above-200MA entry vs the
current VIX-panic entry; (2) ADX>20 filter; (3) VIX-percentile vs absolute-VIX edge; (4) GAP-
REALISTIC stops (fill the -5% stop at the actual gapped close, not a perfect -5%) to quantify how
much the 'optimizer says 3%' result degrades once stops can be blown through. Core = 18-ETF book."""
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


def load(ticker):
    p = yf.download([ticker, "^VIX"], period="max", interval="1d", progress=False, auto_adjust=True)
    s = p["Close"][ticker].dropna(); v = p["Close"]["^VIX"].reindex(s.index).ffill()
    h = p["High"][ticker].reindex(s.index); lo = p["Low"][ticker].reindex(s.index)
    def rsi(n):
        d = s.diff(); g = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
        l = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
        return 100 - 100 / (1 + g / l.replace(0, np.nan))
    tr = pd.concat([(h-lo), (h-s.shift()).abs(), (lo-s.shift()).abs()], axis=1).max(axis=1)
    up = h.diff(); dn = -lo.diff()
    pl = ((up > dn) & (up > 0)) * up; mi = ((dn > up) & (dn > 0)) * dn
    atr = tr.ewm(alpha=1/14, adjust=False).mean()
    pdi = 100*pl.ewm(alpha=1/14, adjust=False).mean()/atr; mdi = 100*mi.ewm(alpha=1/14, adjust=False).mean()/atr
    adx = (100*(pdi-mdi).abs()/(pdi+mdi).replace(0, np.nan)).ewm(alpha=1/14, adjust=False).mean()
    return dict(s=s, v=v, ma5=s.rolling(5).mean(), ma20=s.rolling(20).mean(),
                ma200=s.rolling(200).mean(), rsi14=rsi(14), rsi2=rsi(2), adx=adx,
                vpct=v.rolling(252).apply(lambda x: (x.iloc[-1] > x).mean(), raw=False))


def trades(ticker, entry="vix_panic", gap=False, adx_gate=None):
    d = load(ticker)
    s = d["s"].values; v = d["v"].values; m5 = d["ma5"].values; m20 = d["ma20"].values
    m200 = d["ma200"].values; r14 = d["rsi14"].values; r2 = d["rsi2"].values
    adx = d["adx"].values; vpct = d["vpct"].values; idx = d["s"].index; n = len(s); COST = 0.0010
    if entry == "vix_panic":
        ent = (s < m20*0.975) & (v/np.roll(v, 5)-1 > 0.15) & (r14 < 35)
    elif entry == "rsi2":
        ent = (s > m200) & (r2 < 10)
    elif entry == "rsi2_vix":
        ent = (s > m200) & (r2 < 10) & (v/np.roll(v, 5)-1 > 0.15)
    ent = np.nan_to_num(ent).astype(bool)
    out = []; i = 200
    while i < n - 1:
        ok = ent[i] and (adx_gate is None or (np.isfinite(adx[i]) and
              (adx[i] > 20 if adx_gate == "hi" else adx[i] < 20)))
        if not ok: i += 1; continue
        e = s[i]; j = i + 1; R = None
        while j < n:
            r = s[j]/e - 1.0
            if s[j] >= m5[j] or r >= 0.03: R = r; break
            if r <= -0.05: R = (r if gap else -0.05); break   # gap: real (possibly worse) fill
            if (j - i) >= 10: R = r; break
            j += 1
        if R is None: R = s[min(j, n-1)]/e - 1.0
        out.append({"d": idx[min(j, n-1)], "r": R - COST, "vix": float(v[i]),
                    "vpct": float(vpct[i]) if np.isfinite(vpct[i]) else np.nan})
        i = j + 1
    return out


def unit(trs):
    u = pd.Series(0.0, index=didx)
    for t in trs:
        dt = pd.Timestamp(t["d"]); dt = dt.tz_localize("UTC") if dt.tz is None else dt
        if cs <= dt <= ce: u.iloc[didx.searchsorted(dt)] += t["r"]
    return u


def met(ret):
    eqc = (1+ret).cumprod(); cagr = eqc.iloc[-1]**(1/yrs)-1
    dd = (eqc/eqc.cummax()-1).min()
    mo = eqc.resample("ME").last().pct_change().dropna()
    return cagr, dd, (mo.mean()/mo.std()*np.sqrt(12) if mo.std() > 0 else 0)


def stat(trs):
    r = np.array([t["r"] for t in trs])
    return f"n={len(r):<4}(~{len(r)/((didx[-1]-didx[0]).days/365.25):.1f}/yr) meanR {r.mean()*100:+.2f}% win {np.mean(r>0)*100:.0f}%"


c0 = met(core_ret)
print(f"core 18-ETF: CAGR {c0[0]*100:+.2f}% DD {c0[1]*100:.1f}% Sharpe {c0[2]:.2f}\n")

print("(1) ENTRY: current VIX-panic vs Connors RSI(2)<10>200MA vs combo (SPY):")
for e in ["vix_panic", "rsi2", "rsi2_vix"]:
    tr = trades("SPY", e); cg, dd, sh = met(core_ret + 0.10*unit(tr))
    print(f"  {e:<10} {stat(tr)} | blend CAGR {cg*100:+.2f}% DD {dd*100:.1f}% Sharpe {sh:.2f}")

print("\n(2) ADX>20 vs ADX<20 vs none, on vix_panic entry (SPY):")
for g, lbl in [(None, "none"), ("hi", "ADX>20"), ("lo", "ADX<20")]:
    tr = trades("SPY", "vix_panic", adx_gate=g)
    if tr: print(f"  {lbl:<8} {stat(tr)}")

print("\n(3) VIX-PERCENTILE edge buckets (vix_panic, SPY) — does the edge track pctile like abs level?")
tr = trades("SPY", "vix_panic"); dv = pd.DataFrame(tr).dropna(subset=["vpct"])
for lo, hi, lbl in [(0, .5, "vpct<50"), (.5, .8, "vpct 50-80"), (.8, .9, "vpct 80-90"), (.9, 1.01, "vpct>90")]:
    sub = dv[(dv["vpct"] >= lo) & (dv["vpct"] < hi)]["r"].values
    if len(sub): print(f"  {lbl:<11} n={len(sub):<3} meanR {sub.mean()*100:+.2f}% win {np.mean(sub>0)*100:.0f}%")

print("\n(4) GAP-REALISTIC stops (fill -5% stop at real gapped close) — SPY+QQQ+XLK pooled:")
print(f"  {'risk':>6}{'  IDEAL stop (-5% floor)':>28}{'  GAP-REAL stop':>22}")
for frac in [0.10, 0.20, 0.40]:
    pid = [t for tk in ["SPY", "QQQ", "XLK"] for t in trades(tk, "vix_panic", gap=False)]
    pgp = [t for tk in ["SPY", "QQQ", "XLK"] for t in trades(tk, "vix_panic", gap=True)]
    i_cg, i_dd, i_sh = met(core_ret + frac*unit(pid))
    g_cg, g_dd, g_sh = met(core_ret + frac*unit(pgp))
    print(f"  {frac*0.05*100:>5.1f}%  CAGR {i_cg*100:+.2f}% DD {i_dd*100:.1f}% Sh {i_sh:.2f}"
          f"   |  CAGR {g_cg*100:+.2f}% DD {g_dd*100:.1f}% Sh {g_sh:.2f}")
