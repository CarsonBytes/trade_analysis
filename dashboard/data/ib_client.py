"""Persistent Interactive Brokers client for futures data (and, later, exec).

Mirrors mt5_client's contract: degrade gracefully. If `ib_async` isn't
installed or no TWS / IB Gateway is running, every function returns
None/False and the rest of the app falls back to yfinance -- so this is safe
to ship before IB is actually set up.

ib_async is asyncio-based and NOT safe to call concurrently from our worker
threads, so -- exactly like mt5_client._LOCK -- every call is serialised behind
a single lock and runs on one dedicated event loop.

Setup recap:
  - TWS or IB Gateway installed, logged into the PAPER account, API enabled
    (Configure -> API -> Enable ActiveX and Socket Clients).
  - `uv sync --extra ib` to install ib_async.
  - analyst/.env:  IB_HOST=127.0.0.1  IB_PORT=7497  IB_CLIENT_ID=7  IB_ACCOUNT=DU...
    (7497 = TWS paper, 4002 = Gateway paper. The exec client must use a paper port.)

Returned data SHAPES match mt5_client so providers.py needs only a dispatch:
  get_rates / continuous_rates -> DataFrame[UTC index; open,high,low,close]
  get_tick                     -> {bid,ask,mid,spread,time,age_sec}
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401  -- loads .env (IB_* vars) + TLS, must be first

import os
import threading
import datetime as dt

import pandas as pd

from dashboard.core.log import log
from dashboard.data.contracts import FutureSpec

_LOCK = threading.Lock()
_S: dict = {"ib": None, "import": None, "connected": False}

# IB historical-data bar-size strings, keyed by our timeframe labels.
_BARSIZE = {"M1": "1 min", "M5": "5 mins", "M15": "15 mins", "M30": "30 mins",
            "H1": "1 hour", "H4": "4 hours", "D1": "1 day", "W1": "1 week"}


def _mod():
    """The ib_async module, or None if not installed."""
    if _S["import"] is None:
        try:
            import ib_async  # type: ignore
            _S["import"] = ib_async
        except Exception:
            _S["import"] = False
    return _S["import"] or None


_RECONNECT_THROTTLE_SEC = 30   # after a failed connect, don't retry for this long


def _ensure_conn():
    """Connect once and reuse. Returns the connected IB handle, or None.

    THROTTLED: after a failed connect (gateway down), don't reattempt for
    _RECONNECT_THROTTLE_SEC -- otherwise every dashboard refresh eats an 8s
    connect timeout and the whole UI stalls while IBKR is offline."""
    import time
    ib_async = _mod()
    if ib_async is None:
        return None
    ib = _S["ib"]
    if ib is not None and getattr(ib, "isConnected", lambda: False)():
        return ib
    last_try = _S.get("last_attempt", 0.0)
    if time.time() - last_try < _RECONNECT_THROTTLE_SEC:
        return None                 # recently failed; stay down, don't block
    _S["last_attempt"] = time.time()
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "7497"))
    base_id = int(os.environ.get("IB_CLIENT_ID", "7"))
    # clientId collisions are real (Error 326): a lingering prior connection that
    # didn't disconnect cleanly keeps the id reserved on the Gateway. Try a small
    # window of ids so an ad-hoc probe / crashed run can't lock us out forever.
    last_err = None
    for client_id in range(base_id, base_id + 4):
        try:
            ib = ib_async.IB()
            ib.connect(host, port, clientId=client_id, readonly=True, timeout=8)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "client id" in msg or "326" in msg:
                continue            # id in use -> try the next one
            break                   # other failure (gateway down) -> stop
        _S["ib"], _S["connected"] = ib, True
        log.info("ib_client: connected %s:%s clientId=%s", host, port, client_id)
        return ib
    log.info("ib_client: connect to %s:%s failed (%s) -- falling back", host, port, last_err)
    _S["ib"], _S["connected"] = None, False
    return None


def is_available() -> bool:
    with _LOCK:
        return _ensure_conn() is not None


# ---- contract resolution ---------------------------------------------------

def _qualify_front(ib, spec: FutureSpec, asof: dt.date):
    """Return the qualified dated front-month Future for `spec`, or None.
    Nearest contract whose lastTradeDate is > asof + roll_offset_days bdays."""
    ib_async = _mod()
    base = ib_async.Future(symbol=spec.symbol, exchange=spec.exchange,
                           currency=spec.currency)
    try:
        details = ib.reqContractDetails(base)
    except Exception as e:
        log.info("ib_client: reqContractDetails(%s) failed: %s", spec.symbol, e)
        return None
    from dashboard.data.contracts import _business_days_between
    cands = []
    for d in details or []:
        c = d.contract
        ymd = getattr(c, "lastTradeDateOrContractMonth", "") or ""
        try:
            exp = dt.datetime.strptime(ymd[:8], "%Y%m%d").date() if len(ymd) >= 8 \
                else dt.datetime.strptime(ymd[:6], "%Y%m").date().replace(day=28)
        except ValueError:
            continue
        if _business_days_between(asof, exp) > spec.roll_offset_days:
            cands.append((exp, c))
    if not cands:
        return None
    cands.sort(key=lambda t: t[0])
    return cands[0][1]


def front_future(spec: FutureSpec, asof: dt.date):
    """Public: qualified front-month Contract to TRADE (orders only). None if IB down."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
        return _qualify_front(ib, spec, asof)


# ---- bars ------------------------------------------------------------------

def _bars_to_df(bars) -> pd.DataFrame | None:
    if not bars:
        return None
    rows = [{"time": b.date, "open": float(b.open), "high": float(b.high),
             "low": float(b.low), "close": float(b.close)} for b in bars]
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")[["open", "high", "low", "close"]].astype(float).sort_index()


def _hist(ib, contract, timeframe: str, n: int) -> pd.DataFrame | None:
    import math
    bar = _BARSIZE.get(timeframe, "1 week")
    # IB durationStr is finicky about units: a weekly barSize with a "W" duration
    # (e.g. "60 W") fails with Error 366 -- it must be expressed in YEARS. Daily
    # over ~1y must also roll up to years. Size generously from n bars (+pad).
    if bar == "1 week":
        duration = f"{max(1, math.ceil(n / 52) + 1)} Y"
    elif bar == "1 day":
        duration = f"{n} D" if n <= 365 else f"{math.ceil(n / 252) + 1} Y"
    else:                                    # intraday: keep in days (IB caps long ranges)
        duration = f"{max(1, math.ceil(n / 390))} D"
    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr=duration, barSizeSetting=bar,
            whatToShow="TRADES", useRTH=False, formatDate=2)
    except Exception as e:
        log.info("ib_client: reqHistoricalData(%s) failed: %s",
                 getattr(contract, "symbol", "?"), e)
        return None
    return _bars_to_df(bars)


def continuous_rates(spec: FutureSpec, timeframe: str = "W1", n: int = 320):
    """Back-adjusted CONTINUOUS bars (CONTFUT) for SIGNALS/backtest. None if IB down."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
        ib_async = _mod()
        cont = ib_async.ContFuture(symbol=spec.symbol, exchange=spec.exchange,
                                   currency=spec.currency)
        # MUST qualify first: a bare ContFuture has no conId, so reqHistoricalData
        # fails with Error 366 ("no historical data query found"). Qualifying
        # resolves it to the live front month with a conId.
        try:
            ib.qualifyContracts(cont)
        except Exception as e:
            log.info("ib_client: qualify ContFuture(%s) failed: %s", spec.symbol, e)
            return None
        if not getattr(cont, "conId", 0):
            log.info("ib_client: ContFuture(%s) did not qualify (no conId)", spec.symbol)
            return None
        return _hist(ib, cont, timeframe, n)


def get_rates(spec: FutureSpec, timeframe: str = "D1", n: int = 500,
              asof: dt.date | None = None):
    """Dated FRONT-MONTH OHLC bars (for trade resolution). None if IB down."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
        c = _qualify_front(ib, spec, asof or dt.date.today())
        return _hist(ib, c, timeframe, n) if c is not None else None


def get_tick(spec: FutureSpec) -> dict | None:
    """Latest front-month quote: bid/ask/mid/spread + age. Needs a real-time
    market-data subscription for the exchange; returns None on delayed/empty."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
        c = _qualify_front(ib, spec, dt.date.today())
        if c is None:
            return None
        try:
            t = ib.reqMktData(c, "", True, False)
            ib.sleep(1.0)
        except Exception:
            return None
        bid, ask = getattr(t, "bid", None), getattr(t, "ask", None)
        if not bid or not ask or bid != bid or ask != ask:  # None / NaN
            return None
        bid, ask = float(bid), float(ask)
        ts = pd.Timestamp.now(tz="UTC")
        return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                "spread": ask - bid, "time": ts, "age_sec": 0.0}


# ---- account guard data (used by ib_exec, see §4 of IBKR_SCOPE.md) ---------

def account_id() -> str | None:
    """The connected account id (DU... = paper, U... = live). None if IB down."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
        try:
            accts = ib.managedAccounts()
        except Exception:
            return None
        return accts[0] if accts else None


def is_paper() -> bool:
    """True only when the connected account is an IB PAPER account (DU prefix)
    AND matches IB_ACCOUNT if that env var is set. Non-configurable safety."""
    acct = account_id()
    if not acct or not acct.upper().startswith("DU"):
        return False
    want = os.environ.get("IB_ACCOUNT")
    return (want is None) or (acct == want)


def contract_check(spec: FutureSpec) -> dict | None:
    """Cross-check the curated spec against the broker's contract details:
    compares multiplier and min-tick. Returns {ok, broker_multiplier,
    broker_tick, mismatches}. Run at setup to catch a stale SPECS entry."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
        c = _qualify_front(ib, spec, dt.date.today())
        if c is None:
            return None
        try:
            det = ib.reqContractDetails(c)
        except Exception:
            return None
    if not det:
        return None
    d = det[0]
    bmult = float(getattr(c, "multiplier", 0) or 0)
    btick = float(getattr(d, "minTick", 0) or 0)
    mism = []
    if bmult and abs(bmult - spec.multiplier) > 1e-6:
        mism.append(f"multiplier spec={spec.multiplier} broker={bmult}")
    if btick and abs(btick - spec.tick_size) > 1e-9:
        mism.append(f"tick spec={spec.tick_size} broker={btick}")
    return {"ok": not mism, "broker_multiplier": bmult,
            "broker_tick": btick, "mismatches": mism}


def shutdown() -> None:
    with _LOCK:
        ib = _S["ib"]
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                pass
        _S["ib"], _S["connected"] = None, False


# Disconnect on interpreter exit so a process never strands its clientId on the
# Gateway (the Error 326 collision we hit during verification).
import atexit as _atexit
_atexit.register(shutdown)


# ---- setup helper CLI ------------------------------------------------------

def diagnose() -> None:
    """One-stop check: package present? gateway up? logged in? paper? Plus a
    spec cross-check on a couple of contracts. Mirrors mt5_client.diagnose()."""
    ib_async = _mod()
    if ib_async is None:
        print("[FAIL] ib_async NOT installed -> run:  uv sync --extra ib")
        return
    print(f"[OK] ib_async imported (v{getattr(ib_async, '__version__', '?')})")
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = os.environ.get("IB_PORT", "7497")
    print(f"connecting to {host}:{port} (IB_CLIENT_ID={os.environ.get('IB_CLIENT_ID', '7')})...")
    if not is_available():
        print("[FAIL] could not connect. Is TWS / IB Gateway running with the API")
        print("       enabled, and is IB_PORT the PAPER port (7497 TWS / 4002 Gateway)?")
        return
    acct = account_id()
    print(f"[OK] connected. account={acct}  paper={is_paper()}")
    if acct and not acct.upper().startswith("DU"):
        print("     WARNING: not a DU (paper) account -- ib_exec will refuse to trade.")
    from dashboard.data.contracts import SPECS
    for key in ("MES", "GC", "ZN"):
        spec = SPECS.get(key)
        chk = contract_check(spec) if spec else None
        if chk is None:
            print(f"  {key}: no contract details (market data / symbol?)")
        elif chk["ok"]:
            print(f"  {key}: spec OK (multiplier {spec.multiplier}, tick {spec.tick_size})")
        else:
            print(f"  {key}: SPEC MISMATCH -> {'; '.join(chk['mismatches'])}")
    shutdown()


if __name__ == "__main__":
    diagnose()
