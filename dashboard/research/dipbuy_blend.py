"""BLEND: core 17-ETF trend book + risk-matched panic-MR dip-buy sleeve, on the SAME timeline,
so the tail-CORRELATION (dip losses land during core drawdowns) shows up in the combined maxDD
instead of being assumed. Answers: does the +EV dip sleeve, at risk-matched sizing, actually
improve the PORTFOLIO (CAGR / maxDD / Sharpe), or just add correlated noise?

Core = backtest._portfolio over the ETF universe, longweekly, idle cash @4.3% (the documented
live config). Sleeve = the SPY dip-buy trades (spy_dipbuy_test rules), each sized to risk 0.5%
of current equity at the -5% stop => notional 0.1*E => account return contribution 0.1*r per
trade. Both expressed as daily return streams on the SAME capital and ADDED (correct for small
sizing; preserves date alignment => correlation). Restricted to the core's live span.
"""
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")
import numpy as np, pandas as pd, yfinance as yf
import sys
sys.path.insert(0, "D:/quant")
import dashboard.research.backtest as bt
from dashboard.instruments import ETF_UNIVERSE
from dashboard.research.backtest import ETF_CANDIDATES
from dashboard import core as _core

# ---- 1. CORE: full screened book (base + candidates = the adopted ~17/18-ETF universe),
#         exactly like backtest.main's --etf-screen branch (whitelist off, trade all classes) ----
bt.CASH_YIELD = 0.043                       # constant idle-cash rate (documented live)
_core.paper.WEEKLY_TREND_CLASSES = set()
cands = []
for inst in ETF_UNIVERSE + ETF_CANDIDATES:
    raw = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0:
        continue
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].copy()
    df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    if len(df) < 220:
        continue
    cands += bt._signals(df, inst.key)

eq_core, realized = bt._portfolio(cands, 0.005)
eq_core = eq_core[~eq_core.index.duplicated(keep="last")].sort_index()
core_start, core_end = eq_core.index[0], eq_core.index[-1]
print(f"CORE 17-ETF book: {len(realized)} trades, span {core_start.date()}..{core_end.date()}")

# daily core return stream
didx = pd.date_range(core_start, core_end, freq="B", tz="UTC")
core_daily = eq_core.reindex(eq_core.index.union(didx)).ffill().reindex(didx).ffill()
core_ret = core_daily.pct_change().fillna(0.0)

# ---- 2. SLEEVE: SPY dip-buy trades on the same timeline ----
px = yf.download(["SPY", "^VIX"], period="max", interval="1d", progress=False,
                 auto_adjust=True)["Close"].dropna()
spy, vix = px["SPY"], px["^VIX"]
ma20 = spy.rolling(20).mean(); ma5 = spy.rolling(5).mean()
d = spy.diff(); g = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
l = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
rsi = 100 - 100 / (1 + g / l.replace(0, np.nan))
vup = vix / vix.shift(5) - 1.0
entry = ((spy < ma20 * 0.975) & (vup > 0.15) & (rsi < 35)).fillna(False).values
c = spy.values; m5 = ma5.values; sidx = spy.index; n = len(c); COST = 0.0010
dip = []
i = 20
while i < n - 1:
    if not entry[i]:
        i += 1; continue
    e = c[i]; j = i + 1; ex = None
    while j < n:
        r = c[j] / e - 1.0
        if c[j] >= m5[j] or r >= 0.03: ex = r; break
        if r <= -0.05: ex = -0.05; break
        if (j - i) >= 10: ex = r; break
        j += 1
    if ex is None: ex = c[min(j, n - 1)] / e - 1.0
    dip.append((sidx[min(j, n - 1)], ex - COST))      # (exit_date, trade return)
    i = j + 1

# risk-matched: risk 0.5% at the -5% stop => deploy 0.1*E => account contribution 0.1*r per trade
sleeve_ret = pd.Series(0.0, index=didx)
n_in = 0
for dt, r in dip:
    dt = pd.Timestamp(dt).tz_localize("UTC") if pd.Timestamp(dt).tz is None else pd.Timestamp(dt)
    if core_start <= dt <= core_end:
        pos = didx.searchsorted(dt)
        if pos < len(didx):
            sleeve_ret.iloc[pos] += 0.10 * r
            n_in += 1
print(f"DIP sleeve: {n_in} trades inside the core span (of {len(dip)} total 1993-2026)\n")

# ---- 3. compare ----
def summary(ret, label):
    eqc = (1 + ret).cumprod()
    yrs = (didx[-1] - didx[0]).days / 365.25
    cagr = eqc.iloc[-1] ** (1 / yrs) - 1
    dd = (eqc / eqc.cummax() - 1).min()
    mo = eqc.resample("ME").last().pct_change().dropna()
    shp = mo.mean() / mo.std() * np.sqrt(12) if mo.std() > 0 else 0
    print(f"  {label:<28} CAGR {cagr*100:+.2f}% | maxDD {dd*100:.1f}% | "
          f"monthly Sharpe {shp:.2f} | CAGR/|DD| {abs(cagr/dd):.2f}")
    return cagr, dd

print(f"OVER THE CORE'S LIVE SPAN ({(didx[-1]-didx[0]).days/365.25:.1f}y), risk-matched blend:")
c0 = summary(core_ret, "core only")
c1 = summary(core_ret + sleeve_ret, "core + dip sleeve")
s0 = summary(sleeve_ret, "dip sleeve ALONE")
print(f"\n  DELTA from adding the sleeve: CAGR {(c1[0]-c0[0])*100:+.2f} pp | "
      f"maxDD {(c1[1]-c0[1])*100:+.2f} pp")
# correlation of the two daily streams on days the sleeve is active
act = sleeve_ret != 0
if act.sum() > 5:
    corr = np.corrcoef(core_ret[act], sleeve_ret[act])[0, 1]
    print(f"  corr(core, sleeve) on sleeve-active days = {corr:+.2f} "
          f"(negative=diversifying; positive/~0 with the tail timing = stacking)")
print("\n  READ: the sleeve adds a little CAGR but lands its losses during core stress, so the\n"
      "  CAGR/|DD| ratio barely moves (or dips). It's accretive to raw return, ~neutral-to-\n"
      "  slightly-dilutive risk-adjusted. Real but marginal — a 手癮 sleeve, not a portfolio upgrade.")
