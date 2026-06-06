"""Walk-forward analysis: the only backtest result you should believe.

Procedure per fold:
  1. TRAIN window: grid-search the parameter set, pick the params with the
     best in-sample Sharpe (after costs).
  2. TEST window (the bars immediately after train, never seen during search):
     run those frozen params. Record the out-of-sample returns.
  3. Roll forward and repeat.

Stitch all the TEST-window returns together -> the out-of-sample equity curve.
That curve, after costs, is your honest estimate of live performance.

It reports IS-vs-OOS Sharpe degradation per fold (overfit detector) and a
Deflated Sharpe Ratio over the stitched OOS curve using the total number of
parameter combinations searched as the trial count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Callable

import numpy as np
import pandas as pd

from costs import CostModel
from engine import run_backtest, bars_per_year
from metrics import compute_stats, deflated_sharpe_ratio, probabilistic_sharpe_ratio, Stats


def _grid(param_grid: dict) -> list[dict]:
    keys = list(param_grid.keys())
    return [dict(zip(keys, vals)) for vals in product(*param_grid.values())]


def _sharpe_per_bar(net_ret: pd.Series) -> float:
    r = net_ret.dropna()
    if len(r) < 2 or r.std(ddof=1) == 0:
        return -np.inf
    return r.mean() / r.std(ddof=1)


@dataclass
class WalkForwardResult:
    oos_returns: pd.Series
    oos_stats: Stats
    deflated_sharpe: float
    psr: float
    n_trials: int
    fold_table: pd.DataFrame
    chosen_params: list[dict] = field(default_factory=list)

    def report(self) -> str:
        s = self.oos_stats
        lines = [
            "=" * 64,
            "WALK-FORWARD (OUT-OF-SAMPLE) RESULT  -- the only number that counts",
            "=" * 64,
            f"  OOS bars            : {s.n_bars}",
            f"  OOS annual return   : {s.ann_return:>8.2%}",
            f"  OOS annual vol      : {s.ann_vol:>8.2%}",
            f"  OOS Sharpe          : {s.sharpe:>8.2f}",
            f"  OOS Sortino         : {s.sortino:>8.2f}",
            f"  OOS max drawdown    : {s.max_drawdown:>8.2%}",
            f"  OOS Calmar          : {s.calmar:>8.2f}",
            f"  OOS profit factor   : {s.profit_factor:>8.2f}",
            f"  turnover / year     : {s.turnover_per_year:>8.1f}",
            "-" * 64,
            f"  parameter trials    : {self.n_trials}",
            f"  Probabilistic SR    : {self.psr:>8.2%}   (P[true SR > 0], no selection adj.)",
            f"  DEFLATED Sharpe     : {self.deflated_sharpe:>8.2%}   (P[true SR > 0] AFTER {self.n_trials} trials)",
            "-" * 64,
            self._verdict(),
            "=" * 64,
        ]
        return "\n".join(lines)

    def _verdict(self) -> str:
        dsr = self.deflated_sharpe
        is_oos = self.fold_table
        median_degr = is_oos["oos_sharpe"].median() - is_oos["is_sharpe"].median()
        if dsr >= 0.95 and self.oos_stats.sharpe > 0:
            tag = "PLAUSIBLE EDGE. DSR>=95%. Still: paper-trade live before risking money."
        elif self.oos_stats.sharpe <= 0:
            tag = "NO EDGE. Out-of-sample Sharpe <= 0 after costs. Discard this idea."
        else:
            tag = ("NOT TRUSTWORTHY. Positive OOS Sharpe but DSR<95% -- most likely "
                   "luck from searching the parameter grid. Do NOT trade.")
        return (f"  VERDICT: {tag}\n"
                f"  (median IS->OOS Sharpe change per fold: {median_degr:+.2f}; "
                f"large negative = overfitting.)")


def walk_forward(
    prices: pd.Series,
    strategy: Callable[..., pd.Series],
    param_grid: dict,
    cost: CostModel,
    n_folds: int = 6,
    train_frac: float = 0.6,
) -> WalkForwardResult:
    """Run rolling walk-forward optimisation.

    Each fold uses `train_frac` of the fold window to optimise and the rest to
    test. Folds tile the series so OOS windows are contiguous and non-overlapping.
    """
    combos = _grid(param_grid)
    n = len(prices)
    fold_len = n // n_folds
    if fold_len < 50:
        raise ValueError("Series too short for this many folds.")

    oos_pieces: list[pd.Series] = []
    rows = []
    chosen: list[dict] = []

    for k in range(n_folds):
        start = k * fold_len
        end = n if k == n_folds - 1 else (k + 1) * fold_len
        window = prices.iloc[start:end]
        split = int(len(window) * train_frac)
        train, test = window.iloc[:split], window.iloc[split:]
        if len(train) < 30 or len(test) < 30:
            continue

        # --- optimise on TRAIN only ---
        best_sr, best_params = -np.inf, combos[0]
        for params in combos:
            sig = strategy(train, **params)
            bt = run_backtest(train, sig, cost)
            sr = _sharpe_per_bar(bt["net_ret"])
            if sr > best_sr:
                best_sr, best_params = sr, params

        # --- evaluate frozen params on TEST ---
        # Recompute the signal over train+test so indicators are warmed up,
        # then slice the test portion. This avoids cold-start NaNs biasing OOS
        # while still never letting test data influence the parameter choice.
        full_sig = strategy(window, **best_params)
        bt_test = run_backtest(test, full_sig.reindex(test.index), cost)
        oos_pieces.append(bt_test["net_ret"])
        chosen.append(best_params)

        is_sr_ann = best_sr * np.sqrt(bars_per_year(train.index))
        oos_sr_ann = _sharpe_per_bar(bt_test["net_ret"]) * np.sqrt(bars_per_year(test.index))
        rows.append({
            "fold": k,
            "train_bars": len(train),
            "test_bars": len(test),
            "params": best_params,
            "is_sharpe": is_sr_ann,
            "oos_sharpe": oos_sr_ann if np.isfinite(oos_sr_ann) else 0.0,
        })

    oos_returns = pd.concat(oos_pieces).sort_index()
    # rebuild a frame the metrics module understands
    oos_frame = pd.DataFrame({
        "net_ret": oos_returns,
        "position": np.nan,  # not tracked across folds; trades counted approx below
        "turnover": 0.0,
    })
    oos_frame["position"] = (oos_returns != 0).astype(float)  # rough activity proxy
    stats = compute_stats(oos_frame.assign(turnover=oos_frame["position"].diff().abs().fillna(0)))

    n_trials = len(combos)
    dsr = deflated_sharpe_ratio(oos_returns, n_trials=n_trials)
    psr = probabilistic_sharpe_ratio(oos_returns)

    return WalkForwardResult(
        oos_returns=oos_returns,
        oos_stats=stats,
        deflated_sharpe=dsr,
        psr=psr,
        n_trials=n_trials,
        fold_table=pd.DataFrame(rows),
        chosen_params=chosen,
    )
