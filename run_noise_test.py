"""THE NOISE TEST -- the framework's self-check against self-deception.

It runs your ENTIRE walk-forward pipeline on many independent zero-drift
random walks (markets with provably NO edge). On average the out-of-sample
result must be indistinguishable from zero.

What the output means:
  - mean OOS Sharpe ~ 0, and DSR rarely >= 0.95  -> the framework is honest.
  - consistently POSITIVE OOS Sharpe on noise     -> you have look-ahead bias,
    a costing bug, or your search is strong enough to mine pure noise. FIX IT
    before believing ANY result on real data.

This is the test that separates a backtest you can trust from a random-number
generator that flatters you.

Run:  python run_noise_test.py --trials 30 --strategy ma_crossover
"""
from __future__ import annotations

import argparse
import numpy as np

from costs import RETAIL_FX_MAJOR
from data import synthetic_gbm
from strategies import STRATEGIES
from walkforward import walk_forward


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=30, help="independent noise markets")
    ap.add_argument("--strategy", default="ma_crossover", choices=list(STRATEGIES))
    ap.add_argument("--n", type=int, default=12_000, help="bars per noise market")
    args = ap.parse_args()

    fn, grid = STRATEGIES[args.strategy]
    oos_sharpes, dsrs, false_discoveries = [], [], 0

    print(f"Noise test: {args.trials} driftless random walks, strategy={args.strategy}")
    print("(expect mean OOS Sharpe ~ 0 and very few DSR>=95%)\n")

    for i in range(args.trials):
        prices = synthetic_gbm(n=args.n, mu_annual=0.0, sigma_annual=0.10, seed=1000 + i)
        res = walk_forward(prices, fn, grid, cost=RETAIL_FX_MAJOR, n_folds=5)
        oos_sharpes.append(res.oos_stats.sharpe)
        dsrs.append(res.deflated_sharpe)
        if res.deflated_sharpe >= 0.95 and res.oos_stats.sharpe > 0:
            false_discoveries += 1

    oos = np.array(oos_sharpes)
    print("-" * 60)
    print(f"  mean  OOS Sharpe : {oos.mean():+.3f}   (should be ~0)")
    print(f"  std   OOS Sharpe : {oos.std():.3f}")
    print(f"  max   OOS Sharpe : {oos.max():+.3f}   (luck, not edge)")
    print(f"  false 'edges'    : {false_discoveries}/{args.trials} "
          f"({false_discoveries / args.trials:.0%})  (DSR claimed edge on noise)")
    print("-" * 60)
    # On zero-drift noise, costs make the EXPECTED mean slightly NEGATIVE.
    # That's correct, not a failure. The failure modes are:
    #   (a) a meaningfully POSITIVE mean OOS Sharpe (edge from nowhere), or
    #   (b) DSR repeatedly claiming a real edge (false-discovery rate too high).
    fdr = false_discoveries / args.trials
    if oos.mean() < 0.25 and fdr <= 0.10:
        print("  PASS: framework does not manufacture edge from noise.")
        if oos.mean() < 0:
            print("        (mean is negative because costs bite even on noise -- as it should.)")
    else:
        print("  FAIL: framework is finding 'profit' in noise -> look-ahead/costing bug,")
        print("        or the parameter search is overfitting. Trust nothing until fixed.")


if __name__ == "__main__":
    main()
