"""Test the user's 2026-06-29 'optimized MR dip-buy' entry on SPY, exactly as specified.

ENTRY (all 3 must hold on a daily close):
  1. SPY close > 2.5% BELOW its 20-day MA      (oversold vs trend)
  2. VIX up > 15% vs 5 trading days ago         (panic confirmation)
  3. RSI(14) < 35                               (oversold confirmation)
EXIT (first to trigger, checked on daily close):
  - touch/again-above 5-day MA, OR +3%          (take-profit)
  - -5% from entry                              (stop)
  - 10 trading days elapsed                     (time cap)
Non-overlapping (no new entry while in a trade), so trades are independent.
Cost 0.05%/leg => 0.10% round-trip charged in R-space. OOS split + win/expR check.

Claimed by the proposal: win ~62-65%, avg win +2.8%, avg loss -4.2%. We verify."""
import numpy as np, pandas as pd, yfinance as yf

px = yf.download(["SPY", "^VIX"], period="max", interval="1d", progress=False, auto_adjust=True)["Close"]
px = px.dropna()
spy, vix = px["SPY"], px["^VIX"]
ma20 = spy.rolling(20).mean()
ma5 = spy.rolling(5).mean()
delta = spy.diff()
gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
vix_up = vix / vix.shift(5) - 1.0

entry = (spy < ma20 * 0.975) & (vix_up > 0.15) & (rsi < 35)
entry = entry.fillna(False).values
c = spy.values; m5 = ma5.values; idx = spy.index
COST = 0.0010   # 0.10% round-trip
n = len(c)

trades = []
i = 20
while i < n - 1:
    if not entry[i]:
        i += 1; continue
    e = c[i]
    j = i + 1
    exit_r, hold = None, 0
    while j < n:
        hold = j - i
        r = c[j] / e - 1.0
        if c[j] >= m5[j] or r >= 0.03:        # TP: back to 5MA or +3%
            exit_r = r; break
        if r <= -0.05:                        # SL
            exit_r = -0.05; break
        if hold >= 10:                        # time cap
            exit_r = r; break
        j += 1
    if exit_r is None:
        exit_r = c[min(j, n - 1)] / e - 1.0
    trades.append({"d": idx[i], "r": exit_r - COST, "hold": hold})
    i = j + 1                                  # non-overlapping

if not trades:
    print("No trades triggered."); raise SystemExit
df = pd.DataFrame(trades).set_index("d")
R = df["r"].values
yrs = (idx[-1] - idx[0]).days / 365.25
cut = idx[0] + (idx[-1] - idx[0]) * 0.60
ins = df[df.index <= cut]["r"].values
oos = df[df.index > cut]["r"].values
wins = R[R > 0]; losses = R[R < 0]

def stat(x, lbl):
    if len(x) < 3:
        print(f"  {lbl:<14} n={len(x)} (too few)"); return
    print(f"  {lbl:<14} n={len(x):<4} mean {x.mean()*100:+.2f}% | win {np.mean(x>0)*100:.0f}% "
          f"| total {x.sum()*100:+.1f}% | per-yr-equiv {x.sum()*100/yrs:+.2f}%")

print(f"SPY dip-buy  [{idx[0].date()}..{idx[-1].date()}, {yrs:.1f}y]  triggers: {len(R)} "
      f"(~{len(R)/yrs:.1f}/yr)  avg hold {df['hold'].mean():.1f}d")
stat(R, "full"); stat(ins, "in-sample"); stat(oos, "out-of-sample")
print(f"\n  avg WIN {wins.mean()*100:+.2f}% (claim +2.8%) | avg LOSS "
      f"{losses.mean()*100:+.2f}% (claim -4.2%) | win-rate {np.mean(R>0)*100:.0f}% (claim 62-65%)")
print(f"  expectancy {R.mean()*100:+.3f}%/trade (cost-adjusted) | "
      f"vs just holding SPY {(spy.iloc[-1]/spy.iloc[0])**(1/yrs)*100-100:+.1f}%/yr buy&hold")
print("  NOTE: ~5/yr triggers, tiny capital deployed -> contribution to total wealth is negligible "
      "regardless of sign (the proposal itself concedes ~+300 HKD/yr).")
