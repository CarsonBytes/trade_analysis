"""REFINED stat-arb: address the prior pairs_test's crudeness (10 hand-picked pairs, fixed params).
Proper method: screen a BROAD liquid-ETF pool for pairs that mean-revert IN-SAMPLE (first 60%),
then TRADE only those OUT-OF-SAMPLE (last 40%) -- the honest test of 'do historically-cointegrated
pairs stay tradable?' Realistic liquid-ETF costs. DSR penalised for the number of pairs screened.
If IS-best pairs still lose OOS net of cost, the rejection is robust (cointegration breakdown)."""
import numpy as np, pandas as pd, yfinance as yf, sys
from itertools import combinations
sys.path.insert(0, "D:/quant")
try:
    from metrics import deflated_sharpe_ratio
except Exception:
    deflated_sharpe_ratio = None

POOL = ["SPY","IVV","VOO","QQQ","DIA","IWM","XLK","XLF","XLE","XLV","XLI","XLY","XLP","XLU",
        "XLB","GLD","SLV","GDX","TLT","IEF","SHY","LQD","HYG","JNK","AGG","EFA","EEM","VEA","VWO"]
WIN, Z_IN, Z_OUT, Z_STOP, MAXHOLD = 60, 2.0, 0.5, 3.5, 30
TOPK = 10                      # trade the K best IS pairs OOS
px = yf.download(POOL, period="max", interval="1d", progress=False, auto_adjust=True)["Close"].dropna(how="all")


def pair_trades(a, b, idx_range):
    df = pd.concat([px[a], px[b]], axis=1).dropna()
    df = df.loc[idx_range[0]:idx_range[1]]
    if len(df) < WIN + 50:
        return None
    spread = np.log(df[a]) - np.log(df[b])
    mu = spread.rolling(WIN).mean().shift(1); sd = spread.rolling(WIN).std(ddof=1).shift(1)
    z = (spread - mu) / sd
    sp, zz = spread.values, z.values; out, i, n = [], WIN, len(df)
    while i < n - 1:
        if not np.isfinite(zz[i]) or abs(zz[i]) < Z_IN:
            i += 1; continue
        side = -1 if zz[i] > 0 else 1; entry = sp[i]; j = i + 1
        while j < n:
            if abs(zz[j]) <= Z_OUT or abs(zz[j]) >= Z_STOP or (j - i) >= MAXHOLD:
                break
            j += 1
        j = min(j, n - 1)
        out.append((df.index[i], side * (sp[j] - entry)))   # gross (cost applied later)
        i = j + 1
    return out


t0, t1 = px.index[0], px.index[-1]
cut = t0 + (t1 - t0) * 0.6
pairs = list(combinations([s for s in POOL if s in px.columns], 2))
print(f"pool {len([s for s in POOL if s in px.columns])} ETFs, {len(pairs)} candidate pairs | "
      f"IS {t0.date()}..{cut.date()} / OOS ..{t1.date()}")

# IS: rank pairs by in-sample MR Sharpe (gross)
is_stats = []
for a, b in pairs:
    tr = pair_trades(a, b, (t0, cut))
    if tr and len(tr) >= 10:
        r = np.array([x[1] for x in tr])
        sh = r.mean() / r.std(ddof=1) if r.std() > 0 else 0
        is_stats.append((sh, a, b))
is_stats.sort(reverse=True)
sel = is_stats[:TOPK]
print(f"\nTop {TOPK} pairs by IN-SAMPLE Sharpe (selected to trade OOS):")
for sh, a, b in sel:
    print(f"  {a}/{b:<5} IS gross Sharpe {sh:.2f}")


def oos_eval(cost_rt):
    allr = []
    for _, a, b in sel:
        tr = pair_trades(a, b, (cut, t1))
        if tr:
            allr += [x[1] - cost_rt for x in tr]
    return np.array(allr)


yrs_oos = (t1 - cut).days / 365.25
print(f"\nOOS performance of the IS-selected pairs (the real test):")
print(f"  {'cost/round-trip':>16}{'n':>6}{'meanRet':>10}{'win':>6}{'annSharpe':>11}{'DSR':>6}")
for c in [0.0004, 0.0010, 0.0020]:    # 0.04% (optimistic liquid), 0.10%, 0.20%
    R = oos_eval(c)
    if len(R) < 5:
        print(f"  {c*100:>14.2f}%  n={len(R)} too few"); continue
    sh = R.mean()/R.std(ddof=1)*np.sqrt(len(R)/yrs_oos) if R.std() > 0 else 0
    dsr = deflated_sharpe_ratio(pd.Series(R), n_trials=len(pairs)) if deflated_sharpe_ratio else float('nan')
    print(f"  {c*100:>14.2f}%{len(R):>6}{R.mean()*100:>9.3f}%{np.mean(R>0)*100:>5.0f}%{sh:>11.2f}{dsr*100:>5.0f}%")
print("\n  RULE: adopt only if OOS meanRet>0 net of REALISTIC cost AND DSR>=95% (penalised for "
      f"{len(pairs)} pairs screened). Note near-identical pairs (SPY/IVV/VOO, HYG/JNK, EFA/VEA) "
      "cointegrate strongly but their spread lives INSIDE the bid-ask -> no tradable edge after cost.")
