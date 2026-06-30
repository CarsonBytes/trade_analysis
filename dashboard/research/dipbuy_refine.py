"""Dip-sleeve: (A) OPTIMAL sizing ratio in the blend, and refinements from the 2026-06-29 pitches
that are NOT already-rejected: (B) VIX-level-scaled sizing, (C) apply to QQQ/IWM/XLK, (D) staged
exits. Core = full 18-ETF screened book + cash@4.3% (same as dipbuy_blend). Sleeve sized as a
fraction `frac` of equity per trade (frac=0.10 == risk 0.5% at the -5% stop == 'risk-matched')."""
import os
os.environ.setdefault("BROKER", "ib"); os.environ.setdefault("UNIVERSE", "etf")
import numpy as np, pandas as pd, yfinance as yf, sys
sys.path.insert(0, "D:/quant")
import dashboard.research.backtest as bt
from dashboard.instruments import ETF_UNIVERSE
from dashboard.research.backtest import ETF_CANDIDATES
from dashboard import core as _core

# ---- core curve (correct universe) ----
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


def dip_trades(ticker, exit_mode="base"):
    """Panic dip-buy on `ticker`; VIX-spike gate uses ^VIX. Returns list of dicts."""
    px = yf.download([ticker, "^VIX"], period="max", interval="1d", progress=False,
                     auto_adjust=True)["Close"].dropna()
    s, vix = px[ticker], px["^VIX"]
    ma20 = s.rolling(20).mean(); ma5 = s.rolling(5).mean()
    ma50 = s.rolling(50).mean(); ma200 = s.rolling(200).mean()
    d = s.diff(); g = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = 100 - 100 / (1 + g / l.replace(0, np.nan))
    vup = vix / vix.shift(5) - 1.0
    ent = ((s < ma20 * 0.975) & (vup > 0.15) & (rsi < 35)).fillna(False).values
    c = s.values; m5 = ma5.values; m20 = ma20.values; m50 = ma50.values
    vx = vix.values; idx = s.index; n = len(c); COST = 0.0010
    out = []; i = 50
    while i < n - 1:
        if not ent[i]: i += 1; continue
        e = c[i]
        if exit_mode == "base":
            j = i + 1; R = None
            while j < n:
                r = c[j] / e - 1.0
                if c[j] >= m5[j] or r >= 0.03: R = r; break
                if r <= -0.05: R = -0.05; break
                if (j - i) >= 10: R = r; break
                j += 1
            if R is None: R = c[min(j, n - 1)] / e - 1.0
            R -= COST
        else:  # staged: 50% @5MA/+3%, 30% @20MA/+6%, 20% @50MA/+10%; whole stop -5%; 10d cap
            rem = 1.0; R = 0.0; got1 = got2 = got3 = False; j = i + 1
            while j < n and rem > 0:
                r = c[j] / e - 1.0
                if r <= -0.05: R += rem * -0.05; rem = 0; break
                if not got1 and (c[j] >= m5[j] or r >= 0.03):
                    R += 0.50 * r; rem -= 0.50; got1 = True
                if got1 and not got2 and (c[j] >= m20[j] or r >= 0.06):
                    R += 0.30 * r; rem -= 0.30; got2 = True
                if got2 and not got3 and (c[j] >= m50[j] or r >= 0.10):
                    R += 0.20 * r; rem -= 0.20; got3 = True
                if (j - i) >= 10: R += rem * r; rem = 0; break
                j += 1
            if rem > 0: R += rem * (c[min(j, n - 1)] / e - 1.0)
            R -= COST
        out.append({"d": idx[min(j, n - 1)], "r": R, "vix": float(vx[i])})
        i = j + 1
    return out


def unit_series(trades):
    u = pd.Series(0.0, index=didx)
    for t in trades:
        dt = pd.Timestamp(t["d"]); dt = dt.tz_localize("UTC") if dt.tz is None else dt
        if cs <= dt <= ce:
            u.iloc[didx.searchsorted(dt)] += t["r"]
    return u


def metrics(ret):
    eqc = (1 + ret).cumprod(); cagr = eqc.iloc[-1] ** (1 / yrs) - 1
    dd = (eqc / eqc.cummax() - 1).min()
    mo = eqc.resample("ME").last().pct_change().dropna()
    shp = mo.mean() / mo.std() * np.sqrt(12) if mo.std() > 0 else 0
    return cagr, dd, shp


spy_base = dip_trades("SPY", "base")
unit_spy = unit_series(spy_base)
c0 = metrics(core_ret)
print(f"core 18-ETF: CAGR {c0[0]*100:+.2f}% | maxDD {c0[1]*100:.1f}% | Sharpe {c0[2]:.2f}  [{yrs:.1f}y]\n")

# ===== (A) OPTIMAL SIZING SWEEP =====
print("(A) Dip-sleeve sizing sweep (frac = fraction of equity per trade; risk%=frac*5%):")
print(f"  {'risk/trade':>10}{'~HKD/trade':>12}{'CAGR':>9}{'maxDD':>9}{'Sharpe':>8}{'CAGR/|DD|':>10}")
best = (None, -9)
for frac in [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.60]:
    cagr, dd, shp = metrics(core_ret + frac * unit_spy)
    ratio = abs(cagr / dd) if dd else 0
    risk = frac * 0.05
    hkd = frac / 0.10 * 90000   # 0.10 == ~90K HKD risk-matched
    tag = ""
    if shp > best[1]: best = (frac, shp)
    print(f"  {risk*100:>9.2f}%{hkd:>11,.0f}{cagr*100:>8.2f}%{dd*100:>8.1f}%{shp:>8.2f}{ratio:>10.2f}"
          f"{'   <- risk-matched' if frac==0.10 else ''}")
print(f"  => Sharpe-maximising frac = {best[0]:.2f} (risk {best[0]*0.05*100:.2f}%/trade, "
      f"~{best[0]/0.10*90000:,.0f} HKD)\n")

# ===== (B) VIX-LEVEL-SCALED SIZING =====
print("(B) Does dip edge depend on entry VIX level? (justifies VIX-scaled sizing or not):")
dv = pd.DataFrame(spy_base)
for lo, hi, lbl in [(0, 20, "VIX<20"), (20, 30, "VIX 20-30"), (30, 99, "VIX>30")]:
    sub = dv[(dv["vix"] >= lo) & (dv["vix"] < hi)]["r"].values
    if len(sub):
        print(f"  {lbl:<10} n={len(sub):<3} meanR {sub.mean()*100:+.2f}% | win {np.mean(sub>0)*100:.0f}% "
              f"| std {sub.std()*100:.1f}%")
print()

# ===== (C) MULTI-ASSET =====
print("(C) Same signal on QQQ/IWM/XLK (frequency + edge + blend impact at risk-matched 0.10):")
allt = {"SPY": spy_base}
for tk in ["QQQ", "IWM", "XLK"]:
    tr = dip_trades(tk, "base"); allt[tk] = tr
    r = np.array([t["r"] for t in tr])
    print(f"  {tk}: n={len(r)} (~{len(r)/((didx[-1]-didx[0]).days/365.25):.1f}/yr) "
          f"meanR {r.mean()*100:+.2f}% | win {np.mean(r>0)*100:.0f}%")
# pooled multi-asset sleeve (all four, risk-matched each)
pooled = [t for tk in allt for t in allt[tk]]
unit_pool = unit_series(pooled)
cagr, dd, shp = metrics(core_ret + 0.10 * unit_pool)
print(f"  BLEND core + 4-asset dip @0.10: CAGR {cagr*100:+.2f}% | maxDD {dd*100:.1f}% | Sharpe {shp:.2f} "
      f"(vs SPY-only +{metrics(core_ret+0.10*unit_spy)[0]*100:.2f}%/{metrics(core_ret+0.10*unit_spy)[1]*100:.1f}%/"
      f"{metrics(core_ret+0.10*unit_spy)[2]:.2f})\n")

# ===== (D) STAGED EXIT =====
print("(D) Staged exit (50%@5MA/+3%, 30%@20MA/+6%, 20%@50MA/+10%) vs base, SPY:")
staged = dip_trades("SPY", "staged")
rb = np.array([t["r"] for t in spy_base]); rs = np.array([t["r"] for t in staged])
print(f"  base   : n={len(rb)} meanR {rb.mean()*100:+.2f}% | win {np.mean(rb>0)*100:.0f}% | std {rb.std()*100:.1f}%")
print(f"  staged : n={len(rs)} meanR {rs.mean()*100:+.2f}% | win {np.mean(rs>0)*100:.0f}% | std {rs.std()*100:.1f}%")
cb, ds = metrics(core_ret + 0.10 * unit_spy), metrics(core_ret + 0.10 * unit_series(staged))
print(f"  blend base  : CAGR {cb[0]*100:+.2f}% DD {cb[1]*100:.1f}% Sharpe {cb[2]:.2f}")
print(f"  blend staged: CAGR {ds[0]*100:+.2f}% DD {ds[1]*100:.1f}% Sharpe {ds[2]:.2f}")
