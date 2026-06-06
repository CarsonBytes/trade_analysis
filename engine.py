"""Vectorised backtest engine with look-ahead bias structurally prevented.

THE ONE RULE: a signal computed using information up to and including the
close of bar t may only affect your position from bar t+1 onward. The engine
enforces this by shifting the signal by one bar internally. You cannot turn
this off. This kills the most common and most seductive backtesting lie.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from costs import CostModel


def run_backtest(
    prices: pd.Series,
    signal: pd.Series,
    cost: CostModel,
) -> pd.DataFrame:
    """Run a single-asset backtest.

    prices: close price series (DatetimeIndex).
    signal: desired position in {-1, 0, +1} (or fractional), indexed like prices.
            Interpreted as "the position I decided on using bar t's close".
    cost:   CostModel; charged on every change in position (turnover).

    Returns a DataFrame with per-bar columns: ret, position, turnover,
    cost, net_ret, equity. `equity` is the gross-of-nothing compounded curve
    starting at 1.0, AFTER costs.
    """
    prices = prices.astype(float)
    signal = signal.reindex(prices.index).fillna(0.0).astype(float)

    # Asset simple returns, close-to-close.
    asset_ret = prices.pct_change().fillna(0.0)

    # *** The anti-look-ahead shift. *** Position you actually hold during the
    # return of bar t was decided at t-1. No exceptions.
    position = signal.shift(1).fillna(0.0)

    gross_ret = position * asset_ret

    # Turnover = |change in position|. Each unit of turnover pays per_side cost.
    turnover = position.diff().abs().fillna(position.abs())
    cost_drag = turnover * cost.per_side

    net_ret = gross_ret - cost_drag
    equity = (1.0 + net_ret).cumprod()

    return pd.DataFrame(
        {
            "ret": asset_ret,
            "position": position,
            "turnover": turnover,
            "cost": cost_drag,
            "net_ret": net_ret,
            "equity": equity,
        }
    )


def bars_per_year(index: pd.DatetimeIndex) -> float:
    """Estimate how many bars make up a year, from the median bar spacing.

    Used to annualise Sharpe etc. Robust to gaps (weekends) because it uses
    the median spacing of actually-present bars.
    """
    if len(index) < 3:
        return 252.0
    # Resolution-independent: let pandas give us a real Timedelta. (Avoids the
    # ns-vs-us trap: pandas >=3.0 indexes can be microsecond-resolution, so a
    # raw int64 view does NOT always mean nanoseconds.)
    median_delta = pd.Series(index).diff().median()
    median_seconds = median_delta.total_seconds()
    if not median_seconds or median_seconds <= 0:
        return 252.0
    seconds_per_year = 365.25 * 24 * 3600
    return seconds_per_year / median_seconds
