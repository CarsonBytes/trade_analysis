"""Market-neutral PAIRS mean-reversion backtest (the one untested 'short-term, uncorrelated'
idea). Pre-committed economic pairs + fixed z-thresholds (no per-pair tuning). Spread =
log(A)-log(B); trailing 60d z-score (no look-ahead). Enter |z|>=2, exit |z|<=0.5, stop |z|>=3.5,
time cap 30d. Cost = 0.05%/leg => 0.20% per round-trip (4 legs). OOS split + DSR penalised for
the number of pairs tried."""
import numpy as np, pandas as pd, yfinance as yf
import sys
sys.path.insert(0, "D:/quant")
try:
    from metrics import deflated_sharpe_ratio
except Exception:
    deflated_sharpe_ratio = None

PAIRS = [("GLD","SLV"),("SPY","QQQ"),("SPY","DIA"),("QQQ","DIA"),("SPY","IWM"),
         ("IEF","TLT"),("SHY","IEF"),("EFA","EEM"),("TIP","IEF"),("HYG","IEF")]
WIN, Z_IN, Z_OUT, Z_STOP, MAXHOLD, COST = 60, 2.0, 0.5, 3.5, 30, 0.0005

syms = sorted({s for p in PAIRS for s in p})
raw = yf.download(syms, period="max", interval="1d", progress=False, auto_adjust=True)["Close"]
raw = raw.dropna(how="all")

def trades_for(a, b):
    df = pd.concat([raw[a], raw[b]], axis=1).dropna()
    if len(df) < WIN + 50:
        return []
    spread = np.log(df[a]) - np.log(df[b])
    mu = spread.rolling(WIN).mean().shift(1)
    sd = spread.rolling(WIN).std(ddof=1).shift(1)
    z = (spread - mu) / sd
    out, i, n = [], WIN, len(df)
    sp = spread.values; zz = z.values
    while i < n - 1:
        if not np.isfinite(zz[i]) or abs(zz[i]) < Z_IN:
            i += 1; continue
        side = -1 if zz[i] > 0 else 1            # z>2: short spread; z<-2: long spread
        entry = sp[i]
        j = i + 1
        while j < n:
            if abs(zz[j]) <= Z_OUT or abs(zz[j]) >= Z_STOP or (j - i) >= MAXHOLD:
                break
            j += 1
        j = min(j, n - 1)
        ret = side * (sp[j] - entry) - COST       # 0.20% round-trip cost
        out.append({"entry_date": df.index[i], "r": ret})
        i = j + 1
    return out

all_tr = []
print(f"{'pair':<12}{'n':>5}{'meanRet%':>10}{'win%':>7}{'ann%':>8}")
yrs = (raw.index[-1] - raw.index[0]).days / 365.25
for a, b in PAIRS:
    tr = trades_for(a, b)
    for t in tr:
        t["pair"] = f"{a}/{b}"
    all_tr += tr
    if tr:
        r = np.array([t["r"] for t in tr])
        print(f"{a+'/'+b:<12}{len(r):>5}{r.mean()*100:>10.3f}{np.mean(r>0)*100:>7.0f}"
              f"{r.sum()*100/yrs:>8.2f}")

all_tr.sort(key=lambda t: t["entry_date"])
R = np.array([t["r"] for t in all_tr])
cut = raw.index[0] + (raw.index[-1] - raw.index[0]) * 0.6
ins = np.array([t["r"] for t in all_tr if t["entry_date"] <= cut])
oos = np.array([t["r"] for t in all_tr if t["entry_date"] > cut])
def stat(x, lbl):
    if len(x) < 2: print(f"  {lbl}: n={len(x)} (too few)"); return
    sharpe = x.mean()/x.std(ddof=1)*np.sqrt(len(x)/yrs) if x.std()>0 else 0   # annualised
    print(f"  {lbl:<22} n={len(x):<5} meanRet {x.mean()*100:+.3f}% | win {np.mean(x>0)*100:.0f}% "
          f"| total {x.sum()*100:+.1f}% | annSharpe {sharpe:.2f}")
print("\n=== AGGREGATE (all pairs pooled) ===")
stat(R, "full history")
stat(ins, "in-sample (60%)")
stat(oos, "out-of-sample (40%)")
if deflated_sharpe_ratio is not None and len(oos) > 2:
    d = deflated_sharpe_ratio(pd.Series(oos), n_trials=len(PAIRS))
    print(f"  OOS DSR (penalised for {len(PAIRS)} pairs tried): {d:.0%}")
print(f"\n  trades/yr ~{len(R)/yrs:.0f} | cost charged 0.20%/round-trip | {yrs:.0f}y")
print("  RULE: adopt only if OOS meanRet clearly > 0 AND DSR >= 95% AND it survives higher cost.")
