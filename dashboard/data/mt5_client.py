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

from dashboard.core import net  # noqa: F401  -- loads .env (MT5_* vars) + TLS

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
    # ATTACH to the already-running, logged-in terminal -- do NOT pass
    # login/server here. Passing credentials forces a re-login that resets the
    # access point (e.g. back to HK-Demo), wiping a manual selection. Explicit
    # login is only done by the --ping/--select CLI when you ask for it.
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
                    "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1})
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
        # bound to a sane epoch window: some symbols (esp. weekend / freshly
        # selected) return a garbage tick time that builds a year-3.6M Timestamp
        # and overflows the subtraction below. Out-of-range -> treat age unknown.
        now_s = int(pd.Timestamp.now(tz="UTC").timestamp())
        ts = (pd.to_datetime(secs, unit="s", utc=True)
              if 946_684_800 < secs < now_s + 86_400 else None)  # 2000-01-01 .. now+1d
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


def symbol_digits(symbol: str) -> int | None:
    """Price decimal precision the broker uses for `symbol` (e.g. 5 for EURGBP).
    SL/TP must be rounded to this or the broker's rounded level won't match the
    paper trade's, leaving the paper trade unresolved when the broker stops out."""
    with _LOCK:
        if not _ensure_init() or not _select(symbol):
            return None
        info = _mod().symbol_info(symbol)
        return int(info.digits) if info is not None else None


def data_path() -> str | None:
    """Terminal data directory (where logs/ lives), or None."""
    with _LOCK:
        if not _ensure_init():
            return None
        ti = _mod().terminal_info()
        return getattr(ti, "data_path", None) if ti else None


def reconnect(login: int, password: str, server: str) -> bool:
    """Re-login the running terminal to `server`, forcing it to re-pick an access
    point. Serialised behind the MT5 lock so it can't race other calls."""
    with _LOCK:
        return bool(_mod().login(login, password=password, server=server))


def connection_status() -> dict | None:
    """Live connection quality: server name, ping (ms), connected flag, and
    retransmission rate. None if MT5 isn't available. ping_last is in microsec."""
    with _LOCK:
        if not _ensure_init():
            return None
        mt5 = _mod()
        ti = mt5.terminal_info()
        ai = mt5.account_info()
        if ti is None:
            return None
        return {
            "server": getattr(ai, "server", "?") if ai else "?",
            "ping_ms": round(getattr(ti, "ping_last", 0) / 1000.0, 1),
            "connected": bool(getattr(ti, "connected", False)),
            "retransmission": round(getattr(ti, "retransmission", 0.0), 2),
        }


def ping_servers(candidates: list[str], login: int, password: str,
                 samples: int = 3) -> list[dict]:
    """On-demand: log the running terminal into each candidate SERVER name and
    measure its ping. Returns [{server, ping_ms, ok}] sorted best-first. This
    BRIEFLY drops the connection per candidate, so run it when idle, not on a
    timer. Only servers your account is valid on will connect."""
    import time as _t
    mt5 = _mod()
    if mt5 is None:
        return []
    out = []
    for srv in candidates:
        with _LOCK:
            ok = mt5.login(login, password=password, server=srv)
            ping = None
            if ok:
                pings = []
                for _ in range(samples):
                    ti = mt5.terminal_info()
                    if ti is not None:
                        pings.append(getattr(ti, "ping_last", 0) / 1000.0)
                    _t.sleep(0.3)
                ping = round(min(pings), 1) if pings else None
        out.append({"server": srv, "ping_ms": ping, "ok": bool(ok)})
    out.sort(key=lambda r: (r["ping_ms"] is None, r["ping_ms"] or 1e9))
    return out


def select_best_server(candidates: list[str], login: int, password: str) -> str | None:
    """Ping all candidates and log into the lowest. Returns the chosen server."""
    ranked = [r for r in ping_servers(candidates, login, password) if r["ok"] and r["ping_ms"]]
    if not ranked:
        return None
    best = ranked[0]["server"]
    with _LOCK:
        _mod().login(login, password=password, server=best)
    return best


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


def _ping_cli(servers: list[str], select: bool) -> None:
    """Compare ping across candidate servers (names from the MT5 bottom-right
    connection icon). Needs MT5_LOGIN/MT5_PASSWORD in analyst/.env."""
    if not _ensure_init():
        print("MT5 not available (terminal running + logged in?)."); return
    cur = connection_status()
    print(f"current: {cur['server']} {cur['ping_ms']:.0f}ms "
          f"(retransmission {cur['retransmission']:.0%})\n")
    login = os.environ.get("MT5_LOGIN")
    password = os.environ.get("MT5_PASSWORD", "")
    if not login:
        print("Set MT5_LOGIN and MT5_PASSWORD in analyst/.env to compare/switch "
              "servers (login is required to re-connect to each)."); return
    if not servers:
        print("Pass candidate server names, e.g.:\n"
              "  uv run python -m dashboard.mt5_client --ping ICMarketsSC-Demo ICMarketsSC-Demo02")
        return
    print(f"pinging {len(servers)} server(s) (briefly reconnects each)...")
    ranked = ping_servers(servers, int(login), password)
    for r in ranked:
        p = f"{r['ping_ms']:.0f}ms" if r["ping_ms"] is not None else "no-connect"
        print(f"  {r['server']:<28} {p}")
    if select and ranked and ranked[0]["ok"] and ranked[0]["ping_ms"]:
        best = select_best_server(servers, int(login), password)
        print(f"\nselected lowest-ping server: {best}")
    else:
        # always restore the original connection if we were only measuring
        with _LOCK:
            _mod().login(int(login), password=password, server=cur["server"])
        print(f"\nrestored original server: {cur['server']} "
              "(re-run with --select to switch to the best)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--ping", nargs="*", metavar="SERVER",
                    help="candidate server names to compare ping")
    ap.add_argument("--select", action="store_true",
                    help="with --ping: log into the lowest-ping server")
    args = ap.parse_args()
    if args.ping is not None:
        _ping_cli(args.ping, args.select)
    else:
        diagnose()
