"""Example strategies.

A strategy is just a function: (prices, **params) -> signal in {-1,0,+1},
where signal[t] is the position DECIDED using data up to and including the
close of bar t. The engine handles the t+1 execution shift; do NOT shift here.

These are deliberately simple and well-known precisely so you can watch the
framework refuse to make them profitable out-of-sample after costs. That
refusal is the framework working correctly, not failing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ma_crossover(prices: pd.Series, fast: int = 20, slow: int = 100) -> pd.Series:
    """Long when fast MA > slow MA, short otherwise. The textbook trend follower."""
    if fast >= slow:
        return pd.Series(0.0, index=prices.index)
    f = prices.rolling(fast).mean()
    s = prices.rolling(slow).mean()
    sig = np.where(f > s, 1.0, -1.0)
    out = pd.Series(sig, index=prices.index)
    out[s.isna()] = 0.0  # no position until slow MA is defined
    return out


def rsi_meanrev(prices: pd.Series, period: int = 14, lower: int = 30, upper: int = 70) -> pd.Series:
    """Mean reversion: buy oversold, sell overbought, flat in between."""
    delta = prices.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    sig = pd.Series(0.0, index=prices.index)
    sig[rsi < lower] = 1.0
    sig[rsi > upper] = -1.0
    sig[rsi.isna()] = 0.0
    return sig.ffill().fillna(0.0)


def breakout(prices: pd.Series, lookback: int = 50) -> pd.Series:
    """Donchian-style: long on new N-bar high, short on new N-bar low."""
    hi = prices.rolling(lookback).max()
    lo = prices.rolling(lookback).min()
    sig = pd.Series(0.0, index=prices.index)
    sig[prices >= hi] = 1.0
    sig[prices <= lo] = -1.0
    sig[hi.isna()] = 0.0
    return sig.ffill().fillna(0.0)


# Registry: name -> (function, parameter grid for walk-forward search).
# The grid size IS your trial count. The framework will penalise you for it
# via the Deflated Sharpe Ratio. More search != more confidence.
STRATEGIES = {
    "ma_crossover": (
        ma_crossover,
        {"fast": [10, 20, 50], "slow": [100, 150, 200]},
    ),
    "rsi_meanrev": (
        rsi_meanrev,
        {"period": [7, 14, 21], "lower": [20, 30], "upper": [70, 80]},
    ),
    "breakout": (
        breakout,
        {"lookback": [20, 50, 100]},
    ),
}
