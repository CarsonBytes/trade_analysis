"""Price data providers. Tries MT5 first (if a terminal is running), falls back
to yfinance (free, no terminal). Returns a close-price Series with a
DatetimeIndex, which is exactly what features.compute_facts expects.
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401  -- MUST be first: sets up TLS for yfinance/curl

import os

import pandas as pd

from dashboard.instruments import Instrument
from dashboard.data import mt5_client

# Which broker backs the live data + (later) execution. "mt5" is the proven
# default; "ib" routes data through the IBKR futures layer (ib_client +
# contracts). yfinance stays the fallback for BOTH. Set BROKER=ib in env to
# switch. Kept as a function read so it can be flipped without reimport.
def _broker() -> str:
    return os.environ.get("BROKER", "mt5").lower()


def _from_mt5(inst: Instrument, timeframe: str = "H1", n: int = 1500) -> pd.Series | None:
    df = mt5_client.get_rates(inst.mt5, timeframe, n)
    if df is None or len(df) == 0:
        return None
    s = df["close"].astype(float)
    s.name = "close"
    return s


def _ib_spec(inst: Instrument):
    """The FutureSpec for this instrument's key, or None if it isn't a future."""
    from dashboard.data.contracts import SPECS
    return SPECS.get(inst.key)


def _ib_close(inst: Instrument, timeframe: str, n: int) -> pd.Series | None:
    """Continuous back-adjusted close series for SIGNALS (no roll gaps)."""
    spec = _ib_spec(inst)
    if spec is None:
        return None
    from dashboard.data import ib_client
    df = ib_client.continuous_rates(spec, timeframe=timeframe, n=n)
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
    """Return (close_series, source_label) of WEEKLY closes for signal scoring.
    Weekly time-series momentum is the validated edge (daily is arbitraged away);
    the scorer's MA/RSI/ATR periods are in BARS, so feeding weekly bars makes
    them weekly signals. ~320 weekly bars (~6y) covers the 150-bar long MA.

    DATA-SOURCE SPLIT: under BROKER=ib we SCORE on yfinance (=F continuous weekly --
    exactly the data the strategy was validated on, and fast), and use IBKR for
    EXECUTION ONLY (ib_exec). IB's reqHistoricalData for 21 instruments is ~9s each
    (~min/refresh) and calling ib_async from the dashboard's worker/nicegui threads
    stalls -- so the frequent scoring loop must NOT touch IB."""
    if _broker() == "ib":
        s = _from_yf(inst, period="8y", interval="1wk")
        return (s, "yfinance") if (s is not None and len(s) > 50) else (None, "none")
    s = _from_mt5(inst, "W1", 320)
    if s is not None and len(s) > 200:
        return s, "mt5"
    s = _from_yf(inst, period="8y", interval="1wk")
    if s is not None and len(s) > 50:
        return s, "yfinance"
    return None, "none"


def get_live_price(inst: Instrument) -> tuple[float | None, str, float | None]:
    """Return (price, source, spread). Near-tick from MT5 if available, else the
    last yfinance bar close (delayed). spread is None when unknown."""
    if _broker() == "ib":
        # No IB tick: needs a paid real-time mkt-data sub (we have none), and the
        # request eats a ~6s timeout per instrument every refresh. Use the delayed
        # yfinance bar -- fine for a weekly system. (IB is execution-only.)
        s = _from_yf(inst)
        if s is not None and len(s):
            return float(s.iloc[-1]), "yfinance-bar", None
        return None, "none", None
    tick = mt5_client.get_tick(inst.mt5)
    if tick is not None:
        return tick["mid"], "mt5-tick", tick["spread"]
    s = _from_yf(inst)
    if s is not None and len(s):
        return float(s.iloc[-1]), "yfinance-bar", None
    return None, "none", None


def get_ohlc(inst: Instrument, period: str = "90d", interval: str = "1h") -> pd.DataFrame | None:
    """OHLC bars (open/high/low/close) for trade resolution -- we need high & low
    to know whether SL or TP was touched. MT5 if available, else yfinance.
    interval='1d' must return true DAILY bars: replay/optimize depend on it
    (MT5 M1 bars would silently turn a '5y daily' backtest into ~5 weeks of
    minute data with a 5-bar = 5-minute horizon).

    IB path: resolution uses yfinance =F daily (fast, consistent with the yfinance
    scoring under BROKER=ib). The authoritative close for a mirrored IBKR position
    is the broker's own fill anyway (ib_exec._resolve_from_broker); this bar-based
    path is just the fallback resolver -- no need to pull dated-contract bars from IB."""
    if _broker() == "ib":
        import yfinance as yf
        try:
            df = yf.download(inst.yf, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df is not None and len(df):
                if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                    df.columns = df.columns.get_level_values(0)
                out = df[["Open", "High", "Low", "Close"]].copy()
                out.columns = ["open", "high", "low", "close"]
                return out.dropna().astype(float)
        except Exception:
            pass
        return None
    if interval == "1d":
        years = int(period[:-1]) if period.endswith("y") else 2
        df = mt5_client.get_rates(inst.mt5, "D1", years * 262)
    else:
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
