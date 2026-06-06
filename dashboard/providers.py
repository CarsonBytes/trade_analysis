"""Price data providers. Tries MT5 first (if a terminal is running), falls back
to yfinance (free, no terminal). Returns a close-price Series with a
DatetimeIndex, which is exactly what features.compute_facts expects.
"""
from __future__ import annotations

from . import net  # noqa: F401  -- MUST be first: sets up TLS for yfinance/curl

import pandas as pd

from .instruments import Instrument


def _from_mt5(inst: Instrument, timeframe: str = "H1", n: int = 1500) -> pd.Series | None:
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception:
        return None
    try:
        if not mt5.initialize():
            return None
        tf = {"H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}["H1"]
        rates = mt5.copy_rates_from_pos(inst.mt5, tf, 0, n)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        s = df.set_index("time")["close"].astype(float)
        s.name = "close"
        return s.sort_index()
    except Exception:
        return None
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


def _from_yf(inst: Instrument, period: str = "60d", interval: str = "1h") -> pd.Series | None:
    try:
        import yfinance as yf
        df = yf.download(inst.yf, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or len(df) == 0:
            return None
        close = df["Close"]
        if hasattr(close, "columns"):       # MultiIndex (single-ticker) -> Series
            close = close.iloc[:, 0]
        close = close.dropna().astype(float)
        close.name = "close"
        return close
    except Exception:
        return None


def get_history(inst: Instrument) -> tuple[pd.Series | None, str]:
    """Return (close_series, source_label). source is 'mt5' | 'yfinance' | 'none'."""
    s = _from_mt5(inst)
    if s is not None and len(s) > 200:
        return s, "mt5"
    s = _from_yf(inst)
    if s is not None and len(s) > 50:
        return s, "yfinance"
    return None, "none"
