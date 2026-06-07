"""Persistent MetaTrader 5 client for near-tick interaction.

Designed to degrade gracefully: if the MetaTrader5 package isn't installed or no
terminal is running, every function returns None/False and the rest of the app
falls back to yfinance. So this is safe to ship before MT5 is actually set up.

Connection is opened ONCE and reused. The MetaTrader5 module is not thread-safe,
so every call is serialised behind a lock.

Setup recap (see dashboard/README.md):
  - MT5 terminal installed, logged into an account, Algo Trading enabled.
  - `uv sync --extra mt5` to install the MetaTrader5 package.
  - Optional login via env (analyst/.env): MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH.
    Without them we attach to the already-running terminal.
"""
from __future__ import annotations

from . import net  # noqa: F401  -- loads .env (MT5_* vars) + TLS

import os
import threading
import datetime as dt

import pandas as pd

_LOCK = threading.Lock()
_S = {"mt5": None, "init": False, "available": None, "selected": set()}


def _mod():
    if _S["mt5"] is None:
        try:
            import MetaTrader5 as mt5  # type: ignore
            _S["mt5"] = mt5
        except Exception:
            _S["mt5"] = False
    return _S["mt5"] or None


def _ensure_init() -> bool:
    """Initialise the connection once. Returns True if connected."""
    mt5 = _mod()
    if mt5 is None:
        _S["available"] = False
        return False
    if _S["init"]:
        return True
    kwargs = {}
    if os.environ.get("MT5_PATH"):
        kwargs["path"] = os.environ["MT5_PATH"]
    if os.environ.get("MT5_LOGIN"):
        kwargs.update(login=int(os.environ["MT5_LOGIN"]),
                      password=os.environ.get("MT5_PASSWORD", ""),
                      server=os.environ.get("MT5_SERVER", ""))
    ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
    _S["init"] = bool(ok)
    _S["available"] = bool(ok)
    return _S["init"]


def is_available() -> bool:
    with _LOCK:
        return _ensure_init()


def _select(symbol: str) -> bool:
    """Ensure a symbol is in Market Watch (required before any data call)."""
    mt5 = _mod()
    if symbol in _S["selected"]:
        return True
    if mt5.symbol_select(symbol, True):
        _S["selected"].add(symbol)
        return True
    return False


_TF = {}


def _tf(name: str):
    mt5 = _mod()
    if not _TF:
        _TF.update({"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
                    "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
                    "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
                    "D1": mt5.TIMEFRAME_D1})
    return _TF[name]


# ---- discovery (setup helper) ---------------------------------------------

def find_symbols(keywords=("XAU", "GOLD", "OIL", "WTI", "USOIL", "EUR", "GBP", "JPY")) -> list[str]:
    """List broker symbols matching keywords -- use this to discover the exact
    names your broker uses for Gold/Oil, then put them in instruments.py."""
    with _LOCK:
        if not _ensure_init():
            return []
        mt5 = _mod()
        out = []
        for s in mt5.symbols_get() or []:
            if any(k in s.name.upper() for k in keywords):
                out.append(s.name)
        return sorted(out)


# ---- near-tick price -------------------------------------------------------

def get_tick(symbol: str) -> dict | None:
    """Latest tick: bid/ask/mid/spread + age in seconds. Poll this ~1-2s for a
    near-real-time price. Returns None if unavailable."""
    with _LOCK:
        if not _ensure_init() or not _select(symbol):
            return None
        mt5 = _mod()
        t = mt5.symbol_info_tick(symbol)
        if t is None or (t.bid == 0 and t.ask == 0):
            return None
        bid, ask = float(t.bid), float(t.ask)
        # `time` is epoch SECONDS (reliable); `time_msc` is milliseconds. Use
        # seconds and guard against absurd values. NOTE: MT5 timestamps are in
        # the broker's SERVER timezone, so age can be off by the server's UTC
        # offset (a few hours) -- fine for a coarse freshness indicator.
        secs = int(getattr(t, "time", 0) or 0)
        ts = pd.to_datetime(secs, unit="s", utc=True) if secs > 0 else None
        raw = (pd.Timestamp.now(tz="UTC") - ts).total_seconds() if ts is not None else None
        age = raw if (raw is not None and -2_678_400 < raw < 31_536_000) else None
        return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                "spread": ask - bid, "time": ts, "age_sec": age}


# ---- bars (analysis + resolution fallback) --------------------------------

def get_rates(symbol: str, timeframe: str = "H1", n: int = 1500) -> pd.DataFrame | None:
    """OHLC bars (newest n). Minimum timeframe is M1."""
    with _LOCK:
        if not _ensure_init() or not _select(symbol):
            return None
        mt5 = _mod()
        rates = mt5.copy_rates_from_pos(symbol, _tf(timeframe), 0, n)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")[["open", "high", "low", "close"]].astype(float)
        return df.sort_index()


# ---- true tick history (exact SL/TP resolution) ---------------------------

def get_ticks_range(symbol: str, t0: dt.datetime, t1: dt.datetime) -> pd.DataFrame | None:
    """All ticks between t0 and t1 (bid/ask). Lets us resolve which of SL/TP was
    hit FIRST, exactly -- removing the conservative 'assume SL first' rule."""
    with _LOCK:
        if not _ensure_init() or not _select(symbol):
            return None
        mt5 = _mod()
        ticks = mt5.copy_ticks_range(symbol, t0, t1, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            return None
        df = pd.DataFrame(ticks)
        df["time"] = pd.to_datetime(df.get("time_msc", df["time"] * 1000), unit="ms", utc=True)
        return df.set_index("time")[["bid", "ask"]].astype(float).sort_index()


def shutdown() -> None:
    with _LOCK:
        mt5 = _mod()
        if mt5 and _S["init"]:
            mt5.shutdown()
            _S["init"] = False


# ---- setup helper CLI ------------------------------------------------------

def diagnose() -> None:
    """One-stop check: package present? terminal found? logged in? Prints the
    exact failure so you know whether to fix the install, the terminal, or login."""
    import struct
    print(f"Python bitness: {struct.calcsize('P') * 8}-bit  (must match MT5 = 64-bit)")

    try:
        import MetaTrader5 as mt5  # type: ignore
        print(f"[OK] MetaTrader5 package imported (v{mt5.__version__})")
    except Exception as e:
        print(f"[FAIL] MetaTrader5 package NOT installed: {e}")
        print("       -> run:  uv sync --extra mt5   (then restart the dashboard)")
        return

    kwargs = {}
    if os.environ.get("MT5_PATH"):
        kwargs["path"] = os.environ["MT5_PATH"]
    if os.environ.get("MT5_LOGIN"):
        kwargs.update(login=int(os.environ["MT5_LOGIN"]),
                      password=os.environ.get("MT5_PASSWORD", ""),
                      server=os.environ.get("MT5_SERVER", ""))
    ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
    print(f"initialize() = {ok}   last_error = {mt5.last_error()}")
    if not ok:
        print("       -> Is the MT5 terminal RUNNING and LOGGED IN on this machine?")
        print("       -> If installed in a non-default location, set MT5_PATH in analyst/.env")
        return

    ti, ai = mt5.terminal_info(), mt5.account_info()
    print(f"terminal: {getattr(ti, 'name', None)}  connected={getattr(ti, 'connected', None)}")
    print(f"account:  {getattr(ai, 'login', None)}  server={getattr(ai, 'server', None)}")
    print("Matching symbols (put your Gold/Oil names into instruments.py):")
    for s in find_symbols():
        print("  ", s)
    mt5.shutdown()


if __name__ == "__main__":
    diagnose()
