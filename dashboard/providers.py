"""Price data providers. Tries MT5 first (if a terminal is running), falls back
to yfinance (free, no terminal). Returns a close-price Series with a
DatetimeIndex, which is exactly what features.compute_facts expects.
"""
from __future__ import annotations

from . import net  # noqa: F401  -- MUST be first: sets up TLS for yfinance/curl

import pandas as pd

from .instruments import Instrument
from . import mt5_client


def _from_mt5(inst: Instrument, timeframe: str = "H1", n: int = 1500) -> pd.Series | None:
    df = mt5_client.get_rates(inst.mt5, timeframe, n)
    if df is None or len(df) == 0:
        return None
    s = df["close"].astype(float)
    s.name = "close"
    return s


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


def get_live_price(inst: Instrument) -> tuple[float | None, str, float | None]:
    """Return (price, source, spread). Near-tick from MT5 if available, else the
    last yfinance bar close (delayed). spread is None when unknown."""
    tick = mt5_client.get_tick(inst.mt5)
    if tick is not None:
        return tick["mid"], "mt5-tick", tick["spread"]
    s = _from_yf(inst)
    if s is not None and len(s):
        return float(s.iloc[-1]), "yfinance-bar", None
    return None, "none", None


def get_ohlc(inst: Instrument, period: str = "90d", interval: str = "1h") -> pd.DataFrame | None:
    """OHLC bars (open/high/low/close) for trade resolution -- we need high & low
    to know whether SL or TP was touched. MT5 (M1) if available, else yfinance."""
    df = mt5_client.get_rates(inst.mt5, "M1", 50_000)
    if df is not None and len(df) > 100:
        return df
    try:
        import yfinance as yf
        df = yf.download(inst.yf, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or len(df) == 0:
            return None
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)  # flatten single-ticker MultiIndex
        out = df[["Open", "High", "Low", "Close"]].copy()
        out.columns = ["open", "high", "low", "close"]
        return out.dropna().astype(float)
    except Exception:
        return None
