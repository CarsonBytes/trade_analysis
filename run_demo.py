"""End-to-end demo on synthetic data with a REAL (small) drift.

This shows the full walk-forward pipeline. Swap `synthetic_gbm` for
data.load_mt5(...) or data.load_csv(...) to test your own market.

Run:  python run_demo.py
"""
from __future__ import annotations

import argparse

from costs import RETAIL_FX_MAJOR
from data import synthetic_gbm, load_csv
from strategies import STRATEGIES
from walkforward import walk_forward


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="ma_crossover", choices=list(STRATEGIES))
    ap.add_argument("--csv", default=None, help="optional OHLC csv path")
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if args.csv:
        prices = load_csv(args.csv)
        print(f"Loaded {len(prices)} bars from {args.csv}")
    else:
        # Tiny positive drift so SOMETHING is there to find; costs still bite.
        prices = synthetic_gbm(n=20_000, mu_annual=0.05, sigma_annual=0.10, seed=args.seed)
        print(f"Synthetic GBM, {len(prices)} bars, small +drift (toy market).")

    fn, grid = STRATEGIES[args.strategy]
    res = walk_forward(prices, fn, grid, cost=RETAIL_FX_MAJOR, n_folds=args.folds)

    print(f"\nStrategy: {args.strategy}")
    print(res.report())
    print("\nPer-fold IS vs OOS (annualised Sharpe):")
    print(res.fold_table[["fold", "params", "is_sharpe", "oos_sharpe"]].to_string(index=False))


if __name__ == "__main__":
    main()
