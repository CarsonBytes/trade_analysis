"""Persistent Interactive Brokers client for futures data + paper execution.

Mirrors mt5_client's contract: degrade gracefully. If `ib_async` isn't installed
or no TWS / IB Gateway is running, every function returns None/False and the rest
of the app falls back to yfinance.

THREADING (the important bit): ib_async is asyncio-based and binds its IB() to the
event loop of the thread that created it. The dashboard calls us from worker
threads, so we run a SINGLE dedicated event-loop thread (`_ensure_loop`) that runs
forever -- keeping the IB connection serviced between calls -- and marshal every IB
interaction onto it:
  - `_run(coro)`  for ib_async's async methods (connectAsync / reqHistoricalDataAsync
                  / reqTickersAsync / qualifyContractsAsync / reqContractDetailsAsync)
  - `call(fn)`    for sync, non-blocking ops run on the loop thread (placeOrder,
                  positions(), fills(), accountValues(), managedAccounts()) -- used
                  by ib_exec so order placement is loop-safe too.
Calling ib_async's blocking SYNC wrappers from a worker thread (the old design)
hung the dashboard refresh loop -- that is what this module exists to prevent.

Setup recap:
  - TWS / IB Gateway logged into the PAPER account, API enabled, paper port.
  - analyst/.env:  IB_HOST=127.0.0.1  IB_PORT=4002  IB_CLIENT_ID=7  IB_ACCOUNT=DU...
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401  -- loads .env (IB_* vars) + TLS, must be first

import os
import asyncio
import threading
import datetime as dt
import concurrent.futures

import pandas as pd

from dashboard.core.log import log
from dashboard.data.contracts import FutureSpec

_LOCK = threading.Lock()          # serialises connect + per-call IB access
_S: dict = {"ib": None, "import": None, "connected": False,
            "loop": None, "thread": None, "last_attempt": 0.0}

_BARSIZE = {"M1": "1 min", "M5": "5 mins", "M15": "15 mins", "M30": "30 mins",
            "H1": "1 hour", "H4": "4 hours", "D1": "1 day", "W1": "1 week"}
_RECONNECT_THROTTLE_SEC = 30      # after a failed connect, don't retry for this long


def _mod():
    """The ib_async module, or None if not installed."""
    if _S["import"] is None:
        try:
            import ib_async  # type: ignore
            _S["import"] = ib_async
        except Exception:
            _S["import"] = False
    return _S["import"] or None


# ---- dedicated event-loop thread -------------------------------------------

def _ensure_loop():
    """Start (once) a daemon thread running an asyncio loop FOREVER. ib_async is
    bound to this loop; running it forever keeps the IB connection's background
    tasks (receiving ticks/bars) serviced between our calls. Returns the loop, or
    None if ib_async isn't installed."""
    if _mod() is None:
        return None
    loop = _S.get("loop")
    if loop is not None and not loop.is_closed():
        return loop
    loop = asyncio.new_event_loop()

    def _runner():
        # bind THIS loop as the thread's current loop so ib_async internals that
        # call asyncio.get_event_loop() (not get_running_loop) resolve to it --
        # essential inside nicegui, whose main thread already owns a different loop.
        asyncio.set_event_loop(loop)
        loop.run_forever()
    t = threading.Thread(target=_runner, name="ib-loop", daemon=True)
    t.start()
    _S["loop"], _S["thread"] = loop, t
    return loop


def _run(coro, timeout: float = 30.0):
    """Run a coroutine on the IB loop thread and return its result (raises on
    error/timeout). For ib_async's *Async methods."""
    loop = _ensure_loop()
    if loop is None:
        raise RuntimeError("ib_async not installed")
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def call(fn, timeout: float = 30.0):
    """Run a SYNC callable on the IB loop thread; return its result. For
    non-blocking ib_async ops (placeOrder, positions(), fills(), accountValues(),
    managedAccounts()) so ib_exec can use them safely from a worker thread."""
    loop = _ensure_loop()
    if loop is None:
        raise RuntimeError("ib_async not installed")
    fut: concurrent.futures.Future = concurrent.futures.Future()

    def runner():
        try:
            fut.set_result(fn())
        except Exception as e:                       # noqa: BLE001
            fut.set_exception(e)
    loop.call_soon_threadsafe(runner)
    return fut.result(timeout)


def ib_handle():
    """The connected IB object (for ib_exec, used ONLY inside call()/_run). None if down."""
    return _S["ib"]


# ---- connection ------------------------------------------------------------

def _ensure_conn():
    """Connect once (on the loop thread) and reuse. Returns the IB handle or None.
    Throttled: after a failed connect, don't reattempt for _RECONNECT_THROTTLE_SEC
    so a down gateway doesn't stall every refresh."""
    import time
    ib_async = _mod()
    if ib_async is None:
        return None
    ib = _S["ib"]
    if ib is not None and ib.isConnected():
        return ib
    if time.time() - _S.get("last_attempt", 0.0) < _RECONNECT_THROTTLE_SEC:
        return None
    _S["last_attempt"] = time.time()
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "7497"))
    base_id = int(os.environ.get("IB_CLIENT_ID", "7"))
    last_err = None
    for client_id in range(base_id, base_id + 4):     # clientId collisions (Error 326)
        try:
            ib = ib_async.IB()
            # connect ON the loop thread; readonly=False so ib_exec can place PAPER
            # orders (the DU-paper + paper-port guard in ib_exec is the real safety).
            _run(ib.connectAsync(host, port, clientId=client_id, timeout=8,
                                 readonly=False), timeout=15)
        except Exception as e:                         # noqa: BLE001
            last_err = e
            msg = str(e).lower()
            if "client id" in msg or "326" in msg:
                continue
            break
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
    """Qualified dated front-month Future for `spec` (nearest non-expired beyond
    the roll window), or None. All IB I/O on the loop thread."""
    ib_async = _mod()
    base = ib_async.Future(symbol=spec.symbol, exchange=spec.exchange,
                           currency=spec.currency)
    try:
        details = _run(ib.reqContractDetailsAsync(base))
    except Exception as e:                             # noqa: BLE001
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
    """Public: qualified front-month Contract to TRADE. None if IB down."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
        return _qualify_front(ib, spec, asof)


def stock_contract(symbol: str, currency: str = "USD"):
    """Qualified SMART-routed Stock/ETF contract to TRADE (orders). None if IB down."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
        ib_async = _mod()
        c = ib_async.Stock(symbol, "SMART", currency)
        try:
            _run(ib.qualifyContractsAsync(c))
        except Exception as e:                         # noqa: BLE001
            log.info("ib_client: qualify Stock(%s) failed: %s", symbol, e)
            return None
        return c if getattr(c, "conId", 0) else None


# HKD is PEGGED to USD (7.75-7.85 band) -> a constant is accurate to <1% and is the
# robust fallback when no FX market-data subscription is available.
_PEG_USD_PER = {"HKD": 1.0 / 7.80}


def fx_to_usd(ccy: str) -> float | None:
    """USD per 1 unit of `ccy` (1.0 for USD). For converting a non-USD account's
    equity into USD before sizing US ETFs. Uses DELAYED historical FX (works without a
    real-time sub); falls back to a pegged constant (e.g. HKD). None if all fail."""
    ccy = (ccy or "USD").upper()
    if ccy == "USD":
        return 1.0
    with _LOCK:
        ib = _ensure_conn()
        if ib is not None:
            ib_async = _mod()
            pair = ib_async.Forex(f"{ccy}USD")         # e.g. HKDUSD -> USD per HKD
            try:
                bars = _run(ib.reqHistoricalDataAsync(
                    pair, endDateTime="", durationStr="5 D", barSizeSetting="1 day",
                    whatToShow="MIDPOINT", useRTH=False, formatDate=2), timeout=8)
                if bars and bars[-1].close and bars[-1].close > 0:
                    return float(bars[-1].close)
            except Exception:                          # noqa: BLE001
                pass
    return _PEG_USD_PER.get(ccy)                        # pegged fallback (HKD), else None


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
    # IB durationStr: weekly barSize needs a "Y" duration ("60 W" fails with 366).
    if bar == "1 week":
        duration = f"{max(1, math.ceil(n / 52) + 1)} Y"
    elif bar == "1 day":
        duration = f"{n} D" if n <= 365 else f"{math.ceil(n / 252) + 1} Y"
    else:
        duration = f"{max(1, math.ceil(n / 390))} D"
    try:
        bars = _run(ib.reqHistoricalDataAsync(
            contract, endDateTime="", durationStr=duration, barSizeSetting=bar,
            whatToShow="TRADES", useRTH=False, formatDate=2), timeout=60)
    except Exception as e:                             # noqa: BLE001
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
        try:                                           # qualify -> conId (else 366)
            _run(ib.qualifyContractsAsync(cont))
        except Exception as e:                         # noqa: BLE001
            log.info("ib_client: qualify ContFuture(%s) failed: %s", spec.symbol, e)
            return None
        if not getattr(cont, "conId", 0):
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
    """Latest front-month quote: bid/ask/mid/spread. Needs a real-time market-data
    subscription; returns None on delayed/empty."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
        c = _qualify_front(ib, spec, dt.date.today())
        if c is None:
            return None
        try:
            tickers = _run(ib.reqTickersAsync(c), timeout=6)
        except Exception:                              # noqa: BLE001
            return None
        t = tickers[0] if tickers else None
        bid, ask = getattr(t, "bid", None), getattr(t, "ask", None)
        if not bid or not ask or bid != bid or ask != ask:   # None / NaN
            return None
        bid, ask = float(bid), float(ask)
        return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                "spread": ask - bid, "time": pd.Timestamp.now(tz="UTC"), "age_sec": 0.0}


# ---- account guard data (used by ib_exec, see §4 of IBKR_SCOPE.md) ---------

def account_id() -> str | None:
    """Connected account id (DU... = paper). None if IB down."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
    try:
        accts = call(lambda: ib.managedAccounts())
    except Exception:                                  # noqa: BLE001
        return None
    return accts[0] if accts else None


def account_summary() -> dict | None:
    """Paper account balances for the dashboard: NetLiquidation, cash, available
    funds, buying power, unrealized/realized PnL, gross position value (USD). None
    if IB down. Read on the loop thread via call() (worker-thread safe)."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
    try:
        # accountSummaryAsync REQUESTS + waits -- accountValues() is empty right
        # after connect (populated asynchronously).
        vals = _run(ib.accountSummaryAsync(), timeout=10)
    except Exception:                                  # noqa: BLE001
        return None
    want = {"NetLiquidation", "TotalCashValue", "AvailableFunds", "BuyingPower",
            "UnrealizedPnL", "RealizedPnL", "GrossPositionValue", "ExcessLiquidity",
            "AccruedCash"}
    out: dict = {}
    ccy = None
    for v in vals:
        if v.tag in want:                              # accept the account's base ccy
            try:                                       # (paper acct here is HKD, not USD)
                out[v.tag] = float(v.value)
            except (TypeError, ValueError):
                continue
            if v.currency:
                ccy = v.currency
    if ccy:
        out["_ccy"] = ccy
    return out or None


def is_paper() -> bool:
    """True only when the connected account is an IB PAPER account (DU prefix) AND
    matches IB_ACCOUNT if set. Non-configurable safety."""
    acct = account_id()
    if not acct or not acct.upper().startswith("DU"):
        return False
    want = os.environ.get("IB_ACCOUNT")
    return (want is None) or (acct == want)


def contract_check(spec: FutureSpec) -> dict | None:
    """Cross-check curated spec vs broker contract details (multiplier, min-tick)."""
    with _LOCK:
        ib = _ensure_conn()
        if ib is None:
            return None
        c = _qualify_front(ib, spec, dt.date.today())
        if c is None:
            return None
        try:
            det = _run(ib.reqContractDetailsAsync(c))
        except Exception:                              # noqa: BLE001
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
                call(lambda: ib.disconnect(), timeout=5)
            except Exception:                          # noqa: BLE001
                pass
        _S["ib"], _S["connected"] = None, False


# Disconnect on interpreter exit so a process never strands its clientId (Error 326).
import atexit as _atexit
_atexit.register(shutdown)


# ---- setup helper CLI ------------------------------------------------------

def diagnose() -> None:
    """One-stop check: package present? gateway up? logged in? paper? + spec check."""
    ib_async = _mod()
    if ib_async is None:
        print("[FAIL] ib_async NOT installed -> run:  uv pip install ib_async")
        return
    print(f"[OK] ib_async imported (v{getattr(ib_async, '__version__', '?')})")
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = os.environ.get("IB_PORT", "7497")
    print(f"connecting to {host}:{port} (IB_CLIENT_ID={os.environ.get('IB_CLIENT_ID', '7')})...")
    if not is_available():
        print("[FAIL] could not connect. Gateway running, API enabled, paper port (4002)?")
        return
    print(f"[OK] connected. account={account_id()}  paper={is_paper()}")
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
