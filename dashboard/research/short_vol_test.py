"""Weekly SPY IRON CONDOR (short-volatility / variance-risk-premium) backtest — the user's
2026-06-29 'last untested frontier' pitch (sell delta-0.25 condor every week, harvest theta).

We have NO options chain in-stack, so we MODEL each leg with Black-Scholes using ^VIX as the
implied vol. This is honest: VIX (IV) is normally > realised vol, so the model REPRODUCES the
variance-risk-premium edge the pitch relies on — AND it reproduces the tail (a big weekly move
blows through the short strikes and pays max loss). r=0 (negligible weekly).

Each Monday (first trading day of the ISO week): sell a 4-leg iron condor expiring that week's
last trading day:
  short call @ delta 0.25, long call @ delta 0.10   (call credit spread)
  short put  @ delta 0.25, long put  @ delta 0.10   (put credit spread)
credit = premium received; maxloss = max wing width - credit. Per-trade result in R =
pnl / maxloss (scale-invariant across SPY $25..$600 over 30y). Cost charged as a fraction of
maxloss (frictions on 4 legs in+out). Settle at Friday close (or stop intraweek for the 2x rule).

Variants test the pitch's own risk rules:
  base            hold to expiry, every week
  VIX<=30 filter  skip entries when VIX>30 ('pause in panic')
  2x-credit stop  daily intraweek MTM (reprice w/ that day's VIX); close if loss >= 2x credit
"""
import numpy as np, pandas as pd, yfinance as yf
from scipy.stats import norm
import sys
sys.path.insert(0, "D:/quant")
try:
    from metrics import deflated_sharpe_ratio
except Exception:
    deflated_sharpe_ratio = None

SHORT_DELTA, LONG_DELTA = 0.25, 0.10
COST_FRAC = 0.04          # frictions as fraction of maxloss, round-trip (4 legs); also test 0.08
N_TRIALS = 4

px = yf.download(["SPY", "^VIX"], period="max", interval="1d", progress=False,
                 auto_adjust=True)["Close"].dropna()
spy, vix = px["SPY"].values, px["^VIX"].values
idx = px.index
iso = idx.isocalendar()
wkid = (iso.year.astype(int) * 100 + iso.week.astype(int)).values


def bs(S, K, T, sig, call):
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    d1 = (np.log(S / K) + 0.5 * sig * sig * T) / (sig * np.sqrt(T))
    d2 = d1 - sig * np.sqrt(T)
    if call:
        return S * norm.cdf(d1) - K * norm.cdf(d2)
    return K * norm.cdf(-d2) - S * norm.cdf(-d1)


def strike(S, T, sig, delta, call):
    # invert BS delta (r=0): call delta=N(d1); put delta=N(d1)-1
    d1 = norm.ppf(delta) if call else norm.ppf(1 - delta)
    return S * np.exp(0.5 * sig * sig * T - d1 * sig * np.sqrt(T))


def condor_value(S, T, sig, Kcs, Kcl, Kps, Kpl):
    return ((bs(S, Kcs, T, sig, True) - bs(S, Kcl, T, sig, True)) +
            (bs(S, Kps, T, sig, False) - bs(S, Kpl, T, sig, False)))


# group consecutive rows into ISO weeks
trades = {"base": [], "vixfilt": [], "stop": []}
i, n = 0, len(idx)
while i < n:
    j = i
    while j + 1 < n and wkid[j + 1] == wkid[i]:
        j += 1
    # week spans rows i..j; need >=2 days
    if j > i:
        S0, v0 = spy[i], vix[i] / 100.0
        T0 = max((idx[j] - idx[i]).days, 1) / 365.0
        Kcs = strike(S0, T0, v0, SHORT_DELTA, True)
        Kcl = strike(S0, T0, v0, LONG_DELTA, True)
        Kps = strike(S0, T0, v0, SHORT_DELTA, False)
        Kpl = strike(S0, T0, v0, LONG_DELTA, False)
        credit = condor_value(S0, T0, v0, Kcs, Kcl, Kps, Kpl)
        width = max(Kcl - Kcs, Kps - Kpl)
        maxloss = width - credit
        if maxloss > 1e-6 and credit > 0:
            cost = COST_FRAC * maxloss
            ST = spy[j]
            settle_loss = (np.clip(ST - Kcs, 0, Kcl - Kcs) +
                           np.clip(Kps - ST, 0, Kps - Kpl))
            pnl_hold = credit - settle_loss - cost
            R_hold = pnl_hold / maxloss
            d = idx[j]
            trades["base"].append({"d": d, "R": R_hold})
            if vix[i] <= 30:
                trades["vixfilt"].append({"d": d, "R": R_hold})
            # 2x-credit stop: walk intraweek days, reprice, close if MTM loss >= 2*credit
            R_stop = R_hold
            for k in range(i + 1, j + 1):
                Tk = max((idx[j] - idx[k]).days, 0) / 365.0
                Vk = condor_value(spy[k], Tk, vix[k] / 100.0, Kcs, Kcl, Kps, Kpl)
                if (Vk - credit) >= 2 * credit:        # loss hit 2x credit received
                    R_stop = (credit - Vk - cost) / maxloss
                    break
            trades["stop"].append({"d": d, "R": R_stop})
    i = j + 1

yrs = (idx[-1] - idx[0]).days / 365.25


def report(name, rows, dsr=False):
    s = pd.DataFrame(rows).set_index("d")["R"].sort_index()
    R = s.values
    cut = idx[0] + (idx[-1] - idx[0]) * 0.60
    ins = s[s.index <= cut].values
    oos = s[s.index > cut].values
    cum = np.cumsum(R)
    dd = (cum - np.maximum.accumulate(cum)).min()
    shp = R.mean() / R.std(ddof=1) * np.sqrt(52) if R.std() > 0 else 0
    print(f"\n  {name}: n={len(R)} (~{len(R)/yrs:.0f}/yr)  "
          f"meanR {R.mean():+.3f} | win {np.mean(R>0)*100:.0f}% | annSharpe {shp:.2f}")
    print(f"     IS meanR {ins.mean():+.3f} (win {np.mean(ins>0)*100:.0f}%) | "
          f"OOS meanR {oos.mean():+.3f} (win {np.mean(oos>0)*100:.0f}%)")
    print(f"     worst week {R.min():+.2f}R | best {R.max():+.2f}R | "
          f"cum drawdown {dd:.1f}R (= {abs(dd):.0f} max-loss units wiped) | totalR {R.sum():+.0f}")
    if dsr and deflated_sharpe_ratio is not None and len(oos) > 10:
        print(f"     OOS DSR (n_trials={N_TRIALS}): {deflated_sharpe_ratio(pd.Series(oos), N_TRIALS):.0%}")


print(f"SPY weekly iron condor (BS-modelled on VIX)  [{idx[0].date()}..{idx[-1].date()}, "
      f"{yrs:.1f}y]  short d{SHORT_DELTA}/long d{LONG_DELTA}, cost {COST_FRAC*100:.0f}% of maxloss")
report("base (hold to expiry)", trades["base"], dsr=True)
report("VIX<=30 filter", trades["vixfilt"])
report("2x-credit intraweek stop", trades["stop"], dsr=True)

COST_FRAC2 = 0.08
print(f"\n  --- cost sensitivity: re-charge base at {COST_FRAC2*100:.0f}% of maxloss ---")
base2 = [{"d": t["d"], "R": t["R"] - (COST_FRAC2 - COST_FRAC)} for t in trades["base"]]
report(f"base @ {COST_FRAC2*100:.0f}% cost", base2)

print("\nRULE: 'high win rate' is EXPECTED and PROVES NOTHING for short vol — the test is whether "
      "meanR>0 net of cost AND the tail (worst week / cum drawdown) is survivable vs the +EV. "
      "A curve that earns small steady R then gives back tens of R in one crash week = the classic "
      "'pennies in front of a steamroller'. Compare R-drawdown to ~1yr (~52) of premium.")
