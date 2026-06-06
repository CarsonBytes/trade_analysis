"""Full study: walk-forward every strategy on REAL data, with a buy&hold
benchmark, then cross-check each against the noise test. Exports OOS equity.

Run:  python run_study.py --csv eurusd_daily.csv
"""
from __future__ import annotations

import argparse
import numpy as np
import pandas as pd

from costs import RETAIL_FX_MAJOR
from data import load_csv, synthetic_gbm
from engine import run_backtest
from metrics import compute_stats
from strategies import STRATEGIES
from walkforward import walk_forward


def buy_and_hold(prices: pd.Series) -> pd.Series:
    """Benchmark: always long. If your strategy can't beat this after costs,
    you're paying fees to underperform doing nothing."""
    return pd.Series(1.0, index=prices.index)


def noise_false_discovery_rate(strategy, grid, trials=25, n=3000) -> float:
    """Fraction of pure-noise markets on which DSR wrongly claims an edge."""
    fd = 0
    for i in range(trials):
        p = synthetic_gbm(n=n, mu_annual=0.0, sigma_annual=0.10, seed=5000 + i, freq="1D")
        try:
            r = walk_forward(p, strategy, grid, cost=RETAIL_FX_MAJOR, n_folds=5)
            if r.deflated_sharpe >= 0.95 and r.oos_stats.sharpe > 0:
                fd += 1
        except ValueError:
            pass
    return fd / trials


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="eurusd_daily.csv")
    ap.add_argument("--folds", type=int, default=6)
    args = ap.parse_args()

    prices = load_csv(args.csv)
    print(f"Real data: {args.csv}  ({len(prices)} bars, "
          f"{prices.index[0].date()} -> {prices.index[-1].date()})\n")

    # --- benchmark ---
    bh = run_backtest(prices, buy_and_hold(prices), RETAIL_FX_MAJOR)
    bh_stats = compute_stats(bh)
    print(f"BENCHMARK  buy & hold:  ann {bh_stats.ann_return:+.2%}  "
          f"vol {bh_stats.ann_vol:.2%}  Sharpe {bh_stats.sharpe:+.2f}  "
          f"maxDD {bh_stats.max_drawdown:.2%}")
    print("(this is the bar every strategy must clear AFTER costs)\n")

    bh.to_csv("equity_buyhold.csv", columns=["equity"])

    rows = []
    for name, (fn, grid) in STRATEGIES.items():
        res = walk_forward(prices, fn, grid, cost=RETAIL_FX_MAJOR, n_folds=args.folds)
        fdr = noise_false_discovery_rate(fn, grid)
        s = res.oos_stats
        (1 + res.oos_returns).cumprod().to_csv(f"equity_oos_{name}.csv", header=["equity"])
        rows.append({
            "strategy": name,
            "OOS_ann": s.ann_return,
            "OOS_Sharpe": s.sharpe,
            "OOS_maxDD": s.max_drawdown,
            "trials": res.n_trials,
            "DSR": res.deflated_sharpe,
            "noise_FDR": fdr,
            "verdict": _short_verdict(res),
        })

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200, "display.max_columns", 20)
    print("=" * 96)
    print("WALK-FORWARD RESULTS ON REAL EURUSD (all out-of-sample, after costs)")
    print("=" * 96)
    fmt = df.copy()
    fmt["OOS_ann"] = fmt["OOS_ann"].map(lambda x: f"{x:+.2%}")
    fmt["OOS_Sharpe"] = fmt["OOS_Sharpe"].map(lambda x: f"{x:+.2f}")
    fmt["OOS_maxDD"] = fmt["OOS_maxDD"].map(lambda x: f"{x:.2%}")
    fmt["DSR"] = fmt["DSR"].map(lambda x: f"{x:.0%}")
    fmt["noise_FDR"] = fmt["noise_FDR"].map(lambda x: f"{x:.0%}")
    print(fmt.to_string(index=False))
    print("-" * 96)
    print("Reading it: trust a row ONLY if DSR>=95% AND OOS_Sharpe>0 AND it beats buy&hold.")
    print("noise_FDR is how often this strategy faked an edge on pure noise (want ~0%).")
    print("Equity curves exported to equity_oos_*.csv and equity_buyhold.csv")


def _short_verdict(res) -> str:
    if res.oos_stats.sharpe <= 0:
        return "NO EDGE"
    if res.deflated_sharpe >= 0.95:
        return "plausible"
    return "luck/overfit"


if __name__ == "__main__":
    main()
