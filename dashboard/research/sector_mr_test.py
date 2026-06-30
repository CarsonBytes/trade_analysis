"""Sector-rotation MEAN-REVERSION backtest (the user's 2026-06-29 'new idea').

Idea: among the SPDR sector ETFs, each week buy the N WORST performers of the
past week and hold 1 week (cross-sectional short-term reversion across sectors).

Rigour, matching pairs_test.py / spy_signal_test.py:
  - Non-overlapping WEEKLY bars (W-FRI) => independent returns, clean Sharpe/DSR.
  - Signal at end of week t = trailing 1-week return; realised = week t+1 return
    (NO look-ahead).
  - THE KEY CONTROL: compare worst-N vs an equal-weight-all benchmark. A long-only
    sector basket inherits the equity-risk-premium (sectors drift UP), so a positive
    raw return proves nothing. The cross-sectional EDGE = worst-N minus equal-weight.
    Also report best-N (momentum leg) as the mirror check.
  - Cost charged per rebalance turnover (round-trip). ETFs liquid: 0.05%/leg.
  - OOS split 60/40 by date + DSR penalised for the variants tried.

Two universes:
  A) classic-9 SPDRs from 1998 (max history/power): XLB XLE XLF XLI XLK XLP XLU XLV XLY
  B) the user's exact list: XLK XLF XLE XLV XLI XLY XLU XLRE XLC (XLRE 2015, XLC 2018)
"""
import numpy as np, pandas as pd, yfinance as yf
import sys
sys.path.insert(0, "D:/quant")
try:
    from metrics import deflated_sharpe_ratio
except Exception:
    deflated_sharpe_ratio = None

CLASSIC9 = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]
USERLIST = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLU", "XLRE", "XLC"]
N_PICK = 2          # buy the worst-2 (and check best-2)
LEG_COST = 0.0005   # 0.05% per leg; a fully-rotated 2-name book ~ up to 0.20% round-trip/wk
OOS_FRAC = 0.60     # first 60% in-sample
N_TRIALS = 6        # variants considered (2 universes x {worst,best,eq} ~ generous)


def run(name, syms, leg_cost=LEG_COST):
    raw = yf.download(syms, period="max", interval="1d", progress=False,
                      auto_adjust=True)["Close"]
    raw = raw.dropna(how="all")
    # restrict to the window where the WHOLE panel exists (cross-sectional rank needs all)
    panel = raw.dropna()
    if len(panel) < 300:
        print(f"\n### {name}: only {len(panel)} common rows -- too short"); return None
    wk = panel.resample("W-FRI").last().dropna()
    ret = wk.pct_change()                      # this week's return per sector
    weeks = ret.index
    start, end = wk.index[0].date(), wk.index[-1].date()
    yrs = (wk.index[-1] - wk.index[0]).days / 365.25

    rows_worst, rows_best, rows_eq = [], [], []
    # signal at week t (ret.iloc[t]); realise on week t+1 (ret.iloc[t+1])
    for t in range(1, len(ret) - 1):
        sig = ret.iloc[t]
        if sig.isna().any():
            continue
        nxt = ret.iloc[t + 1]
        if nxt.isna().any():
            continue
        order = sig.sort_values()
        worst = order.index[:N_PICK]
        best = order.index[-N_PICK:]
        d = weeks[t + 1]
        # equal-weight benchmark turns over ~0 (static) -> ~no cost; the picks turn over
        # up to fully each week -> charge round-trip on the bought names.
        rows_worst.append({"d": d, "r": float(nxt[worst].mean()) - 2 * leg_cost})
        rows_best.append({"d": d, "r": float(nxt[best].mean()) - 2 * leg_cost})
        rows_eq.append({"d": d, "r": float(nxt.mean())})

    def series(rows):
        s = pd.DataFrame(rows).set_index("d")["r"]
        return s
    sw, sb, se = series(rows_worst), series(rows_best), series(rows_eq)
    alpha = sw - se          # cross-sectional MR edge, beta-stripped (paired by week)

    cut = wk.index[0] + (wk.index[-1] - wk.index[0]) * OOS_FRAC

    def stat(x, lbl, dsr=False):
        x = x.dropna()
        if len(x) < 10:
            print(f"    {lbl:<26} n={len(x)} (too few)"); return
        shp = x.mean() / x.std(ddof=1) * np.sqrt(52) if x.std() > 0 else 0  # weekly->ann
        line = (f"    {lbl:<26} n={len(x):<5} mean {x.mean()*100:+.3f}%/wk | "
                f"win {np.mean(x>0)*100:.0f}% | ann {(1+x.mean())**52-1:+.1%} | annSharpe {shp:.2f}")
        if dsr and deflated_sharpe_ratio is not None:
            d = deflated_sharpe_ratio(x, n_trials=N_TRIALS)
            line += f" | DSR {d:.0%}"
        print(line)

    print(f"\n### {name}  [{start}..{end}, {yrs:.1f}y, {len(sw)} weekly periods, "
          f"cost {leg_cost*100:.2f}%/leg]")
    print("  RAW worst-2 (long-only dip-buy; INCLUDES market drift):")
    stat(sw, "full"); stat(sw[sw.index <= cut], "in-sample"); stat(sw[sw.index > cut], "OOS")
    print("  best-2 (momentum mirror):")
    stat(sb, "full")
    print("  equal-weight-9 benchmark (the beta to beat):")
    stat(se, "full")
    print("  *** ALPHA = worst-2 minus equal-weight (the cross-sectional MR edge) ***")
    stat(alpha, "full", dsr=True)
    stat(alpha[alpha.index <= cut], "in-sample")
    stat(alpha[alpha.index > cut], "OOS", dsr=True)
    return {"alpha": alpha, "worst": sw, "eq": se}


if __name__ == "__main__":
    run("A) classic-9 SPDRs (max history)", CLASSIC9)
    run("B) user's list (XLRE/XLC newer)", USERLIST)
    print("\n  cost sensitivity on classic-9 alpha (does the edge survive realistic cost?):")
    run("A') classic-9 @ 0.10%/leg", CLASSIC9, leg_cost=0.0010)
    print("\nRULE: adopt only if ALPHA (worst-2 minus eq-weight) OOS mean clearly > 0 AND "
          "DSR>=95% AND survives higher cost. Raw worst-2>0 alone = just being long stocks.")
