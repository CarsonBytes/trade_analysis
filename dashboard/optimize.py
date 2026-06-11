"""'Optimize everything for max profit' -- done honestly.

Sweeps the parameter grid (reward:risk, stop width, trend-strength threshold),
but splits each instrument's history into IN-SAMPLE (first 60%) and OUT-OF-SAMPLE
(last 40%). We pick the config that maximises IN-SAMPLE expectancy -- exactly
what 'optimize for profit' means -- then report its OUT-OF-SAMPLE result and a
Deflated Sharpe that penalises for HOW MANY configs we tried.

The point: in-sample profit is trivial to manufacture. The only number that
counts is out-of-sample, deflated for the search. If the best in-sample config
collapses out-of-sample, the 'optimization' found noise, not edge.

Run:  python -m dashboard.optimize --period 5y
"""
from __future__ import annotations

import argparse
import itertools
import pandas as pd

from metrics import deflated_sharpe_ratio
from .instruments import UNIVERSE
from .providers import get_ohlc
from . import paper, scoring
from .replay import replay_variant

RR_GRID = [1.5, 2.0, 2.5, 3.0, 4.0]
SL_GRID = [1.5, 2.0, 3.0]
STR_GRID = [3, 4, 5]


def _expectancy(rs: list[float]) -> float:
    return sum(rs) / len(rs) if rs else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5y")
    ap.add_argument("--split", type=float, default=0.6)
    args = ap.parse_args()

    scoring.BLOCK_EXHAUSTION_ENTRIES = False
    data = {}
    for inst in UNIVERSE:
        df = get_ohlc(inst, period=args.period, interval="1d")
        if df is not None and len(df) > 300:
            data[inst.key] = df
    configs = list(itertools.product(RR_GRID, SL_GRID, STR_GRID))
    print(f"Optimizing over {len(configs)} configs on {len(data)} instruments, "
          f"period {args.period}. IS={args.split:.0%} / OOS={1-args.split:.0%}.\n")

    rows = []
    for rr, slm, ms in configs:
        paper.SL_ATR_MULT, paper.MIN_STRENGTH = slm, ms
        is_r, oos_r = [], []
        for key, df in data.items():
            cut = int(len(df) * args.split)
            is_r += replay_variant(df.iloc[:cut], key, "ATR", rr)
            oos_r += replay_variant(df.iloc[cut:], key, "ATR", rr)
        rows.append({"rr": rr, "sl": slm, "str": ms,
                     "is_exp": _expectancy(is_r),
                     "oos_exp": _expectancy(oos_r),
                     "oos_n": len(oos_r), "oos_r": oos_r})

    best = max(rows, key=lambda r: r["is_exp"])  # what "maximize profit" picks
    best_oos_dsr = deflated_sharpe_ratio(pd.Series(best["oos_r"]), n_trials=len(configs))

    print("Top 5 configs by IN-SAMPLE expectancy (what optimization would choose):")
    print(f"{'rr':>5}{'sl':>5}{'str':>5}{'IS_expR':>10}{'OOS_expR':>10}{'OOS_n':>7}")
    print("-" * 42)
    for r in sorted(rows, key=lambda r: r["is_exp"], reverse=True)[:5]:
        print(f"{r['rr']:>5}{r['sl']:>5}{r['str']:>5}"
              f"{r['is_exp']:>10.3f}{r['oos_exp']:>10.3f}{r['oos_n']:>7}")
    print("-" * 42)

    # honest cross-check: how does the IS-best do OOS, and is there ANY +OOS config?
    best_oos = max(rows, key=lambda r: r["oos_exp"])
    print(f"\nIS-best config: rr{best['rr']} sl{best['sl']} str{best['str']}")
    print(f"  in-sample  expectancy: {best['is_exp']:+.3f} R")
    print(f"  out-of-sample expectancy: {best['oos_exp']:+.3f} R")
    print(f"  out-of-sample DSR (penalised for {len(configs)} configs): {best_oos_dsr:.0%}")
    print(f"\nBest-possible OOS config (cherry-picked on OOS itself): "
          f"rr{best_oos['rr']} sl{best_oos['sl']} str{best_oos['str']} "
          f"-> OOS expR {best_oos['oos_exp']:+.3f}")
    verdict = ("PASS — a real, search-robust edge" if best_oos_dsr >= 0.95 and best["oos_exp"] > 0
               else "FAIL — optimization found in-sample noise; no edge survives out-of-sample")
    print(f"\nVERDICT: {verdict}")


if __name__ == "__main__":
    main()
