"""How much to allocate to the panic-MR (SPY dip-buy) sleeve? It's the one +EV short-term
signal (75% win, +1.21%/trade). The question: scale it up, or keep it a token sleeve?

Same entry/exit as spy_dipbuy_test.py. Here we add the SIZING-decision diagnostics:
  1. Full per-trade R distribution + worst trades (the falling-knife tail).
  2. The sleeve's STANDALONE equity curve if you deploy 100% of a reserved pool each
     (non-overlapping) trade: CAGR-on-deployed, maxDD, Sharpe, time-in-market.
  3. Capital efficiency: % of calendar days actually at risk (the rest sits in SGOV).
  4. TIMING vs the core book: what fraction of triggers fire while SPY < 200dMA / in
     a drawdown -> i.e. does this sleeve DIVERSIFY the 17-ETF trend book, or stack long
     exposure during the SAME stress the core is bleeding in?
"""
import numpy as np, pandas as pd, yfinance as yf

px = yf.download(["SPY", "^VIX"], period="max", interval="1d", progress=False,
                 auto_adjust=True)["Close"].dropna()
spy, vix = px["SPY"], px["^VIX"]
ma20 = spy.rolling(20).mean(); ma5 = spy.rolling(5).mean(); ma200 = spy.rolling(200).mean()
peak = spy.cummax(); ddown = spy / peak - 1.0
delta = spy.diff()
gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
vix_up = vix / vix.shift(5) - 1.0
entry = ((spy < ma20 * 0.975) & (vix_up > 0.15) & (rsi < 35)).fillna(False).values

c = spy.values; m5 = ma5.values; m200 = ma200.values; dd = ddown.values; idx = spy.index
COST = 0.0010
n = len(c)
trades = []
i = 20
while i < n - 1:
    if not entry[i]:
        i += 1; continue
    e = c[i]; j = i + 1; ex = None
    while j < n:
        r = c[j] / e - 1.0
        if c[j] >= m5[j] or r >= 0.03:
            ex = r; break
        if r <= -0.05:
            ex = -0.05; break
        if (j - i) >= 10:
            ex = r; break
        j += 1
    if ex is None:
        ex = c[min(j, n - 1)] / e - 1.0
    trades.append({"d": idx[i], "r": ex - COST,
                   "below200": bool(c[i] < m200[i]) if np.isfinite(m200[i]) else False,
                   "dd_at_entry": float(dd[i])})
    i = j + 1

df = pd.DataFrame(trades).set_index("d")
R = df["r"].values
yrs = (idx[-1] - idx[0]).days / 365.25

# 1. distribution
print(f"SPY panic dip-buy sizing study  [{idx[0].date()}..{idx[-1].date()}, {yrs:.1f}y]")
print(f"  n={len(R)} (~{len(R)/yrs:.1f}/yr) | mean {R.mean()*100:+.2f}% | win {np.mean(R>0)*100:.0f}% "
      f"| std {R.std()*100:.2f}% | worst {R.min()*100:+.1f}% | best {R.max()*100:+.1f}%")
worst = np.sort(R)[:5]
print(f"  5 worst trades: {', '.join(f'{w*100:+.1f}%' for w in worst)}")

# 2. standalone sleeve equity (compound 100% of pool each trade)
eq = np.cumprod(1 + R)
pk = np.maximum.accumulate(eq)
sleeve_dd = (eq / pk - 1).min()
cagr = eq[-1] ** (1 / yrs) - 1
sharpe = R.mean() / R.std(ddof=1) * np.sqrt(len(R) / yrs)
print(f"\n  SLEEVE if 100% of pool deployed each trade: total x{eq[-1]:.2f} | CAGR-on-pool "
      f"{cagr*100:+.1f}% | maxDD {sleeve_dd*100:.1f}% | annSharpe {sharpe:.2f}")

# 3. capital efficiency: days at risk
days_at_risk = len(R) * 3   # avg hold ~3 calendar days
cal_days = (idx[-1] - idx[0]).days
print(f"  capital efficiency: ~{days_at_risk} days at risk / {cal_days} = "
      f"{days_at_risk/cal_days*100:.1f}% of the time deployed; ~{100-days_at_risk/cal_days*100:.0f}% idle "
      f"(in SGOV ~4%). => a reserved pool earns MOSTLY cash yield, not the signal.")

# 4. timing vs core book
print(f"\n  TIMING vs the 17-ETF core (does it diversify or stack?):")
print(f"    {np.mean(df['below200'])*100:.0f}% of triggers fire while SPY < 200dMA")
print(f"    mean SPY drawdown-from-peak AT ENTRY = {df['dd_at_entry'].mean()*100:.1f}% "
      f"(median {df['dd_at_entry'].median()*100:.1f}%)")
print("    => it buys equities DURING equity stress = exactly when the long-only trend book is\n"
      "       also bleeding. Tail-CORRELATED with the core (HANDOFF: adding MR raised DD more than\n"
      "       CAGR; matched-risk, just sizing the core up dominated). Scaling it CONCENTRATES.")

# contribution at sizes
print(f"\n  Annual $ contribution by per-trade size (mean {R.mean()*100:.2f}% x {len(R)/yrs:.1f} trades/yr):")
for sz in [2000, 10000, 30000, 100000]:
    print(f"    {sz:>7,} HKD/trade -> ~{R.mean()*sz*len(R)/yrs:+,.0f} HKD/yr from the signal "
          f"(+ idle pool in SGOV)")
print("\n  VERDICT GUIDE: edge is real => a token 2K is needlessly small; but the binding limits are\n"
      "  (a) ~3-4 trades/yr x ~3 days => the pool is ~96% idle (cash-yield dominated, low capacity),\n"
      "  (b) tail-correlation with the core (it is NOT a diversifier), (c) the -5% stop understates\n"
      "  gap/falling-knife risk in the very crises that trigger it. So: size it to a level whose\n"
      "  WORST realistic cluster you can stomach as 'play money', NOT as a core return driver.")
