"""Walk-forward / rolling-window validation. The strategy has fixed, hand-set parameters
(no fitting step), so this isn't a train/retest-per-fold ML walk-forward -- it's a check of
TEMPORAL stability: does the edge hold up consistently across distinct historical regimes,
or is the headline Sharpe concentrated in one lucky period? Splits the full history into
6 rolling windows and reports per-window CAGR/DD/ratio/expR with the SAME fixed
(already-adopted) parameters throughout.

RESULT (2026-07-09, 22-ETF book, --pos-cap 0.25, 30.3y span): 5/6 windows profitable
(expR positive), but ratio std (0.684) exceeds its own mean (0.877) -- a much WIDER spread
than "narrow, strong robustness" would look like. Meaningfully weaker in 1996-2011
(CAGR +1.2% to +4.6%, one negative window 2001-2006) than 2016-2026 (CAGR +11-12%).
Honest read: the edge is real but regime-dependent, not uniformly stable -- see
HANDOFF.md 2026-07-09 for the full table.
"""
import os
os.environ.setdefault("BROKER", "ib"); os.environ.setdefault("UNIVERSE", "etf")
import numpy as np, pandas as pd, yfinance as yf, sys
sys.path.insert(0, "D:/quant")
import dashboard.research.backtest as bt
from dashboard.instruments import active_universe

bt.CASH_YIELD = None
bt.POS_CAP = 0.25
universe = active_universe()

cands = []
for inst in universe:
    raw = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0: continue
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].copy(); df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None: df.index = df.index.tz_localize("UTC")
    if len(df) < 220: continue
    cands += bt._signals(df, inst.key)

cands_sorted = sorted(cands, key=lambda c: c["entry_date"])
start, end = cands_sorted[0]["entry_date"], cands_sorted[-1]["entry_date"]
total_years = (end - start).days / 365.25
print(f"Full span: {start.date()} to {end.date()} ({total_years:.1f}y), n={len(cands_sorted)}")

N_WINDOWS = 6
window_days = (end - start) / N_WINDOWS

results = []
for i in range(N_WINDOWS):
    w_start = start + window_days * i
    w_end = start + window_days * (i + 1)
    sub = [c for c in cands_sorted if w_start <= c["entry_date"] < w_end]
    if len(sub) < 5:
        print(f"  window {i+1} ({w_start.date()}-{w_end.date()}): n={len(sub)} too few, skip")
        continue
    eq, real = bt._portfolio(sub, 0.005)
    yrs = max(bt._span_years(sub), 0.1)
    m = bt._metrics(eq, real, yrs)
    stats = bt.paper.stats(real)
    ratio = abs(m["cagr"] / m["maxdd"]) if m["maxdd"] else 0
    results.append({"cagr": m["cagr"], "dd": m["maxdd"], "ratio": ratio,
                    "expR": stats["expectancy_R"], "n": len(sub), "win": stats["win_rate"]})
    print(f"  window {i+1} ({w_start.date()} to {w_end.date()}): n={len(sub):<4} "
          f"CAGR={m['cagr']*100:+.2f}% maxDD={m['maxdd']*100:.2f}% ratio={ratio:.3f} "
          f"expR={stats['expectancy_R']:+.3f} win={stats['win_rate']*100:.0f}%")

cagrs = np.array([r["cagr"] for r in results])
ratios = np.array([r["ratio"] for r in results])
expRs = np.array([r["expR"] for r in results])
print(f"\nAcross {len(results)} windows:")
print(f"  CAGR:  mean {cagrs.mean()*100:+.2f}%  std {cagrs.std()*100:.2f}pp  "
      f"min {cagrs.min()*100:+.2f}%  max {cagrs.max()*100:+.2f}%  "
      f"negative windows: {int((cagrs<0).sum())}/{len(results)}")
print(f"  ratio: mean {ratios.mean():.3f}  std {ratios.std():.3f}")
print(f"  expR:  mean {expRs.mean():+.3f}  std {expRs.std():.3f}  "
      f"negative windows: {int((expRs<0).sum())}/{len(results)}")
