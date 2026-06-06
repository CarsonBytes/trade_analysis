"""Data sources.

Three ways in:
  1. synthetic_gbm()  - geometric Brownian motion. Zero-drift = pure noise,
     which is exactly what you feed the framework to check it can't "find"
     profit where none exists.
  2. load_csv()       - generic OHLC CSV.
  3. load_mt5()       - pull bars straight from a running MT5 terminal.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def synthetic_gbm(
    n: int = 20_000,
    mu_annual: float = 0.0,
    sigma_annual: float = 0.10,
    bars_per_year: float = 252 * 24,  # hourly-ish
    start_price: float = 1.10,
    seed: int | None = None,
    freq: str = "1h",
) -> pd.Series:
    """Geometric Brownian motion close-price series.

    mu_annual=0.0 gives a driftless random walk: a market with NO edge.
    Any strategy that shows positive out-of-sample Sharpe on this is mining
    noise (or your engine leaks future info). This is the noise test's fuel.
    """
    rng = np.random.default_rng(seed)
    dt = 1.0 / bars_per_year
    drift = (mu_annual - 0.5 * sigma_annual**2) * dt
    shock = sigma_annual * np.sqrt(dt) * rng.standard_normal(n)
    log_ret = drift + shock
    price = start_price * np.exp(np.cumsum(log_ret))
    idx = pd.date_range("2015-01-01", periods=n, freq=freq)
    return pd.Series(price, index=idx, name="close")


def load_csv(path: str, time_col: str = "time", close_col: str = "close") -> pd.Series:
    """Load a close series from a CSV with a parseable time column."""
    df = pd.read_csv(path)
    df[time_col] = pd.to_datetime(df[time_col])
    s = df.set_index(time_col)[close_col].astype(float)
    s.name = "close"
    return s.sort_index()


def load_mt5(symbol: str, timeframe: str = "H1", n: int = 50_000) -> pd.Series:
    """Pull the last `n` bars from a running MetaTrader 5 terminal.

    Requires the MetaTrader5 package and a logged-in terminal on the same
    Windows machine. Kept import-local so the rest of the framework runs
    without MT5 installed.
    """
    import MetaTrader5 as mt5  # type: ignore

    tf_map = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
    try:
        rates = mt5.copy_rates_from_pos(symbol, tf_map[timeframe], 0, n)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No data for {symbol} {timeframe}: {mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        s = df.set_index("time")["close"].astype(float)
        s.name = "close"
        return s.sort_index()
    finally:
        mt5.shutdown()
