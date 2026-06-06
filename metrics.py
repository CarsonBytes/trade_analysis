"""Performance and *overfitting* metrics.

Ordinary metrics (Sharpe, drawdown) tell you how good a curve looks.
The overfitting metrics (Deflated Sharpe, PSR) tell you how much to believe
it given how hard you searched. The second group is what keeps you honest.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import numpy as np
import pandas as pd
from scipy import stats

from engine import bars_per_year


@dataclass
class Stats:
    n_bars: int
    n_trades: int
    ann_return: float
    ann_vol: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float
    win_rate: float
    profit_factor: float
    turnover_per_year: float

    def as_dict(self) -> dict:
        return asdict(self)


def _trades_from_position(position: pd.Series) -> int:
    """Count round trips: each time position changes counts as activity;
    a 'trade' here = a change to a non-zero position from a different state."""
    changes = position.diff().fillna(position).abs() > 0
    return int(changes.sum())


def compute_stats(bt: pd.DataFrame) -> Stats:
    """Compute performance stats from an engine.run_backtest output frame."""
    r = bt["net_ret"].dropna()
    ppy = bars_per_year(bt.index)

    if len(r) == 0 or r.std(ddof=1) == 0:
        return Stats(len(r), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    ann_return = (1.0 + r).prod() ** (ppy / len(r)) - 1.0
    ann_vol = r.std(ddof=1) * np.sqrt(ppy)
    sharpe = (r.mean() / r.std(ddof=1)) * np.sqrt(ppy)

    downside = r[r < 0]
    dd_std = downside.std(ddof=1) if len(downside) > 1 else np.nan
    sortino = (r.mean() / dd_std) * np.sqrt(ppy) if dd_std and dd_std > 0 else 0.0

    equity = (1.0 + r).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    max_dd = float(drawdown.min())

    calmar = ann_return / abs(max_dd) if max_dd < 0 else 0.0

    wins = r[r > 0]
    losses = r[r < 0]
    win_rate = len(wins) / (len(wins) + len(losses)) if (len(wins) + len(losses)) else 0.0
    profit_factor = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else np.inf

    turnover_py = bt["turnover"].sum() * (ppy / len(r))

    return Stats(
        n_bars=len(r),
        n_trades=_trades_from_position(bt["position"]),
        ann_return=float(ann_return),
        ann_vol=float(ann_vol),
        sharpe=float(sharpe),
        sortino=float(sortino),
        max_drawdown=max_dd,
        calmar=float(calmar),
        win_rate=float(win_rate),
        profit_factor=float(profit_factor),
        turnover_per_year=float(turnover_py),
    )


# ----------------------------------------------------------------------------
# Overfitting-aware metrics
# ----------------------------------------------------------------------------

def probabilistic_sharpe_ratio(returns: pd.Series, benchmark_sr: float = 0.0) -> float:
    """PSR: probability that the TRUE Sharpe exceeds `benchmark_sr`, given the
    sample length, skew and kurtosis. A high in-sample Sharpe over few, fat-
    tailed, skewed bars is far less trustworthy than the raw number suggests.

    Returns a probability in [0, 1]. (Sharpe here is per-bar, non-annualised.)
    """
    r = returns.dropna()
    n = len(r)
    if n < 10 or r.std(ddof=1) == 0:
        return 0.0
    sr = r.mean() / r.std(ddof=1)
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))  # non-excess
    denom = np.sqrt(1 - skew * sr + ((kurt - 1) / 4) * sr**2)
    if denom <= 0:
        return 0.0
    z = (sr - benchmark_sr) * np.sqrt(n - 1) / denom
    return float(stats.norm.cdf(z))


def deflated_sharpe_ratio(
    returns: pd.Series,
    n_trials: int,
    trial_sharpes: list[float] | None = None,
) -> float:
    """Deflated Sharpe Ratio (Bailey & López de Prado, 2014).

    The core anti-self-deception metric. If you tried `n_trials` parameter
    combinations and kept the best, the best Sharpe is inflated purely by luck.
    DSR is the probability the strategy's true Sharpe > 0 AFTER correcting for
    that selection. Rule of thumb: DSR < 0.95 -> don't believe it.

    trial_sharpes: the per-bar Sharpes of all configurations you tried. Used to
    estimate the variance of trials; if omitted, a conservative assumption is
    made from the kept strategy alone.
    """
    r = returns.dropna()
    n = len(r)
    if n < 10 or r.std(ddof=1) == 0:
        return 0.0
    sr = r.mean() / r.std(ddof=1)

    # Expected maximum Sharpe under the null (all trials have true SR = 0),
    # via the expected value of the max of n_trials standard normals.
    if trial_sharpes is not None and len(trial_sharpes) > 1:
        var_trials = float(np.var(trial_sharpes, ddof=1))
    else:
        var_trials = 1.0 / (n - 1)  # conservative fallback

    emc = 0.5772156649  # Euler-Mascheroni
    e = np.e
    m = max(int(n_trials), 1)
    if m == 1:
        sr0 = 0.0
    else:
        z1 = stats.norm.ppf(1 - 1.0 / m)
        z2 = stats.norm.ppf(1 - 1.0 / (m * e))
        expected_max_z = (1 - emc) * z1 + emc * z2
        sr0 = np.sqrt(var_trials) * expected_max_z

    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))
    denom = np.sqrt(1 - skew * sr + ((kurt - 1) / 4) * sr**2)
    if denom <= 0:
        return 0.0
    z = (sr - sr0) * np.sqrt(n - 1) / denom
    return float(stats.norm.cdf(z))
