"""Test the SIGNALS behind the proposed options modules on SPY price data (the part we
CAN validate without an options chain). If the signal has no directional edge, the option
overlay can't either -- it just adds theta + spread cost on top."""
import numpy as np, pandas as pd, yfinance as yf

spy = yf.download("SPY", period="max", interval="1d", progress=False, auto_adjust=True)
if hasattr(spy.columns, "nlevels") and spy.columns.nlevels > 1:
    spy.columns = spy.columns.get_level_values(0)
c = spy["Close"].dropna()
ma50 = c.rolling(50).mean()
above = c > ma50
# "first break" = transition from above to below the 50-day MA
breaks = above.shift(1) & (~above)
bd = c.index[breaks.fillna(False)]
print(f"SPY {c.index[0].date()}..{c.index[-1].date()}  | first-50dMA-break events: {len(bd)}")

def fwd(days):
    rs = []
    for d in bd:
        i = c.index.get_loc(d)
        if i + days < len(c):
            rs.append(c.iloc[i + days] / c.iloc[i] - 1.0)
    rs = np.array(rs)
    return rs

print("\n=== LEAPS thesis: buy PUTS after first 50dMA break -> need SPY to FALL ===")
for lbl, days in [("1mo", 21), ("3mo", 63), ("9mo", 189)]:
    r = fwd(days)
    print(f"  {lbl:>4} fwd: mean {r.mean():+.2%} | median {np.median(r):+.2%} | "
          f"P(up) {np.mean(r>0):.0%} | P(down>10%) {np.mean(r < -0.10):.0%} | "
          f"P(down>15%) {np.mean(r < -0.15):.0%}")
print("  (a put buyer NEEDS the down cases; if mean fwd is POSITIVE, buying puts here bleeds)")

print("\n=== Weekly thesis: SPY>5dMA + ADX>25 -> next-week direction edge ===")
# ADX(14) daily
h, l = spy["High"], spy["Low"]
tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
up = h.diff(); dn = -l.diff()
plus = ((up > dn) & (up > 0)) * up
minus = ((dn > up) & (dn > 0)) * dn
n = 14
atr = tr.ewm(alpha=1/n, adjust=False).mean()
pdi = 100 * plus.ewm(alpha=1/n, adjust=False).mean() / atr
mdi = 100 * minus.ewm(alpha=1/n, adjust=False).mean() / atr
dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
adx = dx.ewm(alpha=1/n, adjust=False).mean()
ma5 = c.rolling(5).mean()
fwd5 = c.shift(-5) / c - 1.0
long_sig = (c > ma5) & (adx > 25)
# directional: long when >5MA, short when <5MA; signed next-week return
signed = np.where(c > ma5, fwd5, -fwd5)
mask = (adx > 25) & fwd5.notna() & ma5.notna()
sr = pd.Series(signed, index=c.index)[mask].dropna()
print(f"  n={len(sr)} signal-days | mean signed 5d return {sr.mean():+.3%} | "
      f"win {np.mean(sr>0):.0%} | median {np.median(sr):+.3%}")
print("  (a weekly debit spread needs this to clearly exceed ~spread+commission cost, "
      "~10-20% of a $256 bet ~ +0.3-0.5% of underlying move just to break even)")
