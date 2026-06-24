"""IBKR paper-account auto-execution: mirrors the paper journal to a real IB
PAPER account so forward trades get real futures fills, commissions and rolls.

The futures analog of executor.py (MT5). Same public surface so service.py can
dispatch on BROKER: mirror_new / sync_closures / live_positions / reconcile.

HARD SAFETY (mirrors executor.is_demo): every entry point checks the connected
account is an IB PAPER account (id starts 'DU', and matches IB_ACCOUNT if set)
AND that we connected on a paper port. Refuses to act otherwise. Non-negotiable
and non-configurable -- a flag flip must never reach a live account.

What it does, per refresh cycle:
  - mirror_new():    for each newly OPEN paper trade of the live variant, resolve
                     the dated FRONT MONTH, SIZE BY SPECS (contracts.choose_contract,
                     micro fallback, skip-if-too-big), and place a bracket order
                     (parent market + attached stop + limit, OCA). Failed sends
                     (market closed, no contract) are retried next cycle.
  - sync_closures(): resolve paper trades from IB fills (broker truth). PLUS roll:
                     if an open position's contract enters its roll window, close
                     the front and re-open the next month carrying the trade.
  - reconcile():     join IB executions back onto the journal -> realized R vs
                     paper R.  CLI: uv run python -m dashboard.ib_exec
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401

import os
import sqlite3
import datetime as dt

import pandas as pd

from dashboard.core import paper
from dashboard.data import ib_client
from dashboard.data import contracts
from dashboard.instruments import FUT_BY_KEY
from dashboard.core.log import log

MIRROR_METHOD = "ATR rr3.0"   # the one live variant we execute (same as MT5 exec)


# ---- paper guard (non-negotiable) ------------------------------------------

def is_paper() -> bool:
    """True only when the connected IB account is a paper account."""
    return ib_client.is_paper()


def _guard():
    """Return the connected IB handle iff we're on a paper account, else None."""
    if not ib_client.is_available():
        return None
    if not is_paper():
        log.warning("ib_exec: connected account is NOT paper -- refusing to trade")
        return None
    # belt-and-suspenders: a paper account must also be reached via a paper port.
    port = int(os.environ.get("IB_PORT", "7497"))
    if port not in (7497, 4002):
        log.warning("ib_exec: IB_PORT %s is not a paper port -- refusing to trade", port)
        return None
    with ib_client._LOCK:
        return ib_client._ensure_conn()


# ---- mirror bookkeeping (same sqlite file as the journal) -------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(paper._DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS ib_mirror (
        paper_id INTEGER UNIQUE, perm_id INTEGER, con_id INTEGER,
        local_symbol TEXT, qty REAL, risk_money REAL, expiry TEXT,
        ts TEXT, status TEXT, note TEXT)""")
    return c


def _mirrored_ids() -> set[int]:
    with paper._LOCK, _conn() as c:
        return {r[0] for r in c.execute("SELECT paper_id FROM ib_mirror").fetchall()}


# ---- actions ----------------------------------------------------------------

def mirror_new() -> list[str]:
    """Place paper bracket orders for OPEN paper trades of the live variant not
    yet mirrored. Returns human-readable log lines."""
    ib = _guard()
    if ib is None:
        return []
    done = _mirrored_ids()
    logs: list[str] = []
    equity = _equity_usd(ib)                    # USD (US futures/ETFs price in USD)
    from dashboard.instruments import ETF_TRADED_BY_KEY
    for t in paper.open_trades():
        if t["method"] != MIRROR_METHOD or t["id"] in done:
            continue
        spec = contracts.SPECS.get(t["instrument"])
        if spec is not None:
            msg = _place_bracket(ib, t, spec, equity)                  # futures
        elif t["instrument"] in ETF_TRADED_BY_KEY:
            msg = _place_etf_bracket(ib, t, equity)                    # ETF (shares)
        else:
            continue
        if msg:
            logs.append(msg); log.info("ib_exec: %s", msg)
    return logs


def _equity_usd(ib) -> float:
    """Net liquidation value in USD. The paper account base may be non-USD (HKD here);
    US futures/ETFs price in USD, so we convert -- WITHOUT this, sizing compared an HKD
    budget to USD risk and would oversize ~8x. Falls back to the configured notional."""
    summ = ib_client.account_summary()
    if summ and summ.get("NetLiquidation") is not None:
        nl = summ["NetLiquidation"]
        rate = ib_client.fx_to_usd(summ.get("_ccy", "USD"))
        if rate:
            return nl * rate
    return paper.ACCOUNT


def _place_bracket(ib, t: dict, spec: contracts.FutureSpec, equity: float) -> str | None:
    inst = FUT_BY_KEY.get(t["instrument"])
    contract = ib_client.front_future(spec, dt.date.today())
    if contract is None:
        return f"{t['instrument']}: no front contract (market data?), retry"
    stop_points = abs(float(t["entry"]) - float(t["sl"]))
    chosen, qty = contracts.choose_contract(spec, equity, stop_points,
                                            paper.RISK_PER_TRADE)
    if qty < 1:
        return f"{t['instrument']}: 1 contract risks > budget even as micro, SKIP"
    # if sizing picked the micro sibling, re-resolve the contract for THAT symbol
    if chosen.key != spec.key:
        contract = ib_client.front_future(chosen, dt.date.today())
        if contract is None:
            return f"{t['instrument']}: no front for micro {chosen.symbol}, retry"
    ib_async = ib_client._mod()
    action = "BUY" if t["direction"] == "long" else "SELL"
    risk_money = equity * paper.RISK_PER_TRADE
    def send():
        bracket = ib.bracketOrder(action, qty, limitPrice=0.0,
                                  takeProfitPrice=float(t["tp"]),
                                  stopLossPrice=float(t["sl"]))
        # parent as MARKET (bracketOrder makes a LMT parent by default)
        bracket.parent.orderType = "MKT"
        bracket.parent.lmtPrice = 0.0
        for o in bracket:
            o.tif = "GTC"
            o.orderRef = f"quant#{t['id']}"
        return [ib.placeOrder(contract, o) for o in bracket]   # non-blocking; loop transmits
    try:
        trades = ib_client.call(send, timeout=15)
    except Exception as e:                     # noqa: BLE001
        return f"{t['instrument']}: order send failed ({e}), retry"
    parent = trades[0].order
    perm_id = getattr(parent, "permId", 0)
    with paper._LOCK, _conn() as c:
        c.execute("INSERT OR IGNORE INTO ib_mirror VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (t["id"], perm_id, getattr(contract, "conId", 0),
                   getattr(contract, "localSymbol", chosen.symbol), qty, risk_money,
                   getattr(contract, "lastTradeDateOrContractMonth", ""),
                   dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "OPEN", ""))
    return (f"{t['instrument']}: paper bracket placed {action} {qty}x "
            f"{getattr(contract, 'localSymbol', chosen.symbol)} "
            f"SL {t['sl']} TP {t['tp']}")


def _place_etf_bracket(ib, t: dict, equity_usd: float) -> str | None:
    """ETF order: SHARE-based sizing (shares = floor(risk_$ / stop_per_share)) and a
    SMART stock bracket. No contract specs/rolls -- ETFs are simpler than futures and
    divide finely, so any account size works."""
    contract = ib_client.stock_contract(t["instrument"])      # symbol == key (GLD, SPY…)
    if contract is None:
        return f"{t['instrument']}: no stock contract (market data?), retry"
    stop_per_share = abs(float(t["entry"]) - float(t["sl"]))
    qty = contracts.size_shares(equity_usd, stop_per_share, paper.RISK_PER_TRADE)
    if qty < 1:
        return f"{t['instrument']}: <1 share at the risk budget, SKIP"
    action = "BUY" if t["direction"] == "long" else "SELL"
    risk_money = equity_usd * paper.RISK_PER_TRADE
    # US ETFs trade on a $0.01 tick; IB rejects (Error 110) child legs whose price
    # carries sub-penny precision, leaving the position UNPROTECTED. Round to cents.
    tp_px = round(float(t["tp"]), 2)
    sl_px = round(float(t["sl"]), 2)

    def send():
        bracket = ib.bracketOrder(action, qty, limitPrice=0.0,
                                  takeProfitPrice=tp_px,
                                  stopLossPrice=sl_px)
        bracket.parent.orderType = "MKT"
        bracket.parent.lmtPrice = 0.0
        for o in bracket:
            o.tif = "GTC"
            o.orderRef = f"quant#{t['id']}"
        return [ib.placeOrder(contract, o) for o in bracket]
    try:
        trades = ib_client.call(send, timeout=15)
    except Exception as e:                     # noqa: BLE001
        return f"{t['instrument']}: order send failed ({e}), retry"
    perm_id = getattr(trades[0].order, "permId", 0)
    with paper._LOCK, _conn() as c:
        c.execute("INSERT OR IGNORE INTO ib_mirror VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (t["id"], perm_id, getattr(contract, "conId", 0), t["instrument"],
                   qty, risk_money, "",
                   dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "OPEN", "etf"))
    return (f"{t['instrument']}: paper bracket placed {action} {qty}sh "
            f"SL {sl_px} TP {tp_px}")


def sync_closures() -> list[str]:
    """Resolve paper trades from IB fills (broker truth) and roll positions whose
    contract is entering its expiry window."""
    ib = _guard()
    if ib is None:
        return []
    logs: list[str] = []
    journal = {t["id"]: t for t in paper.all_trades()}
    with paper._LOCK, _conn() as c:
        rows = c.execute("SELECT paper_id, con_id, local_symbol, qty, expiry, "
                         "status FROM ib_mirror").fetchall()
    def _snapshot():
        ib.reqAllOpenOrders()                      # populate orders from any session
        working = {t.contract.conId for t in (ib.openTrades() or [])
                   if t.orderStatus.status in
                   ("ApiPending", "PendingSubmit", "PreSubmitted", "Submitted")}
        return {p.contract.conId: p for p in (ib.positions() or [])}, working
    positions, working_conids = ib_client.call(_snapshot)
    for paper_id, con_id, local_symbol, qty, expiry, mstatus in rows:
        pt = journal.get(paper_id)
        if pt is None or mstatus == "CLOSED":
            continue
        open_pos = positions.get(con_id)
        # GUARD: "no position" can mean NOT-YET-FILLED (parent order still working,
        # e.g. placed while the market was closed), NOT just exited. Only treat it as
        # closed when there is ALSO no working order for this contract -- otherwise the
        # trade is pending and would be wrongly closed before it ever fills.
        if (open_pos is None or open_pos.position == 0) and con_id in working_conids:
            continue
        # (a) position closed at broker (SL/TP filled) while paper still OPEN ->
        #     resolve the paper trade from the broker's actual exit.
        if open_pos is None or open_pos.position == 0:
            with paper._LOCK, _conn() as c:
                c.execute("UPDATE ib_mirror SET status='CLOSED' WHERE paper_id=?",
                          (paper_id,))
            if pt["status"] == "OPEN":
                msg = _resolve_from_broker(ib, pt, con_id)
                if msg:
                    logs.append(msg); log.info("ib_exec: %s", msg)
            continue
        # (b) roll: open position inside its contract's roll window -> close front,
        #     re-open next month carrying the same paper trade.
        spec = contracts.SPECS.get(pt["instrument"])
        exp = _parse_expiry(expiry)
        if spec is not None and exp is not None and contracts.needs_roll(exp, spec):
            msg = _roll_position(ib, pt, spec, con_id, qty)
            if msg:
                logs.append(msg); log.info("ib_exec: %s", msg)
    return logs


def _resolve_from_broker(ib, trade: dict, con_id: int) -> str | None:
    """Resolve a paper trade from its mirrored position's actual fills."""
    exit_price = _last_exit_price(ib, con_id)
    if exit_price is None:
        return None
    # futures cost: real commission + tick slippage (price points), not the CFD
    # half-spread fraction.
    spec = contracts.SPECS.get(trade["instrument"])
    cost_abs = contracts.cost_points(spec) if spec else None
    r = paper.r_multiple(trade["direction"], trade["entry"], trade["sl"], exit_price,
                         half_spread=trade.get("half_spread") or paper.HALF_SPREAD,
                         cost_abs=cost_abs)
    status = "WIN" if r > 0 else "LOSS"
    exit_ts = pd.Timestamp.now(tz="UTC")
    paper._update_resolution(trade["id"], status, str(exit_ts), exit_price,
                             round(r, 3), exit_reason="closed at broker (IB)")
    return (f"#{trade['id']} {trade['instrument']} resolved from BROKER (IB): "
            f"{status} R={r:+.2f} exit={exit_price}")


def _last_exit_price(ib, con_id: int) -> float | None:
    """Average price of the most recent closing fill for con_id, or None."""
    fills = ib_client.call(lambda: [f for f in (ib.fills() or [])
                                    if getattr(f.contract, "conId", None) == con_id])
    if not fills:
        return None
    return float(fills[-1].execution.avgPrice or fills[-1].execution.price)


def _roll_position(ib, trade: dict, spec: contracts.FutureSpec, old_con_id: int,
                   qty: float) -> str | None:
    """Close the expiring front contract and re-open the next month at market,
    keeping the same SL/TP and paper-trade id. Updates the mirror row."""
    new_contract = ib_client.front_future(spec, dt.date.today() +
                                          dt.timedelta(days=spec.roll_offset_days + 7))
    if new_contract is None or getattr(new_contract, "conId", 0) == old_con_id:
        return None  # no next month resolvable yet; try again next cycle
    ib_async = ib_client._mod()
    close_act = "SELL" if trade["direction"] == "long" else "BUY"
    open_act = "BUY" if trade["direction"] == "long" else "SELL"
    def do_roll():
        old = next((p.contract for p in (ib.positions() or [])
                    if p.contract.conId == old_con_id), None)
        if old is not None:
            ib.placeOrder(old, ib_async.MarketOrder(close_act, qty))
        parent = ib_async.MarketOrder(open_act, qty)
        sl = ib_async.StopOrder(close_act, qty, float(trade["sl"]))
        tp = ib_async.LimitOrder(close_act, qty, float(trade["tp"]))
        for o in (parent, sl, tp):
            o.orderRef = f"quant#{trade['id']}"; o.tif = "GTC"
        ib.placeOrder(new_contract, parent)
        ib.placeOrder(new_contract, sl)
        ib.placeOrder(new_contract, tp)
    try:
        ib_client.call(do_roll, timeout=15)
    except Exception as e:                     # noqa: BLE001
        return f"#{trade['id']} {trade['instrument']}: ROLL FAILED ({e}), retry"
    with paper._LOCK, _conn() as c:
        c.execute("UPDATE ib_mirror SET con_id=?, local_symbol=?, expiry=?, "
                  "note='rolled' WHERE paper_id=?",
                  (getattr(new_contract, "conId", 0),
                   getattr(new_contract, "localSymbol", spec.symbol),
                   getattr(new_contract, "lastTradeDateOrContractMonth", ""),
                   trade["id"]))
    return (f"#{trade['id']} {trade['instrument']}: ROLLED to "
            f"{getattr(new_contract, 'localSymbol', spec.symbol)}")


# ---- read-only views (UI / analysis) ---------------------------------------

def live_positions() -> dict:
    """Map paper_id -> live IB position for OUR trades (matched via the
    ib_mirror table's con_id). Mirrors executor.live_positions output shape."""
    if not ib_client.is_available():
        return {}
    ib = ib_client._ensure_conn()
    with paper._LOCK, _conn() as c:
        rows = c.execute("SELECT paper_id, con_id, qty FROM ib_mirror "
                         "WHERE status='OPEN'").fetchall()
    positions, portfolio = ib_client.call(lambda: (
        {p.contract.conId: p for p in (ib.positions() or [])},
        {i.contract.conId: i for i in (ib.portfolio() or [])}))
    out: dict[int, dict] = {}
    for paper_id, con_id, qty in rows:
        p = positions.get(con_id)
        if p is None or p.position == 0:
            continue
        pf = portfolio.get(con_id)
        out[paper_id] = {
            "ticket": con_id, "open": float(p.avgCost or 0),
            "profit": float(pf.unrealizedPNL) if pf else 0.0,
            "volume": float(abs(p.position)),
            "direction": "long" if p.position > 0 else "short"}
    return out


def reconcile() -> list[dict]:
    """Per mirrored trade: paper R vs realized IB R from fills (PnL incl.
    commission, normalised by the risk money at placement)."""
    if not ib_client.is_available():
        return []
    ib = ib_client._ensure_conn()
    with paper._LOCK, _conn() as c:
        rows = c.execute("SELECT paper_id, con_id, qty, risk_money, status "
                         "FROM ib_mirror").fetchall()
    journal = {t["id"]: t for t in paper.all_trades()}
    fills = ib_client.call(lambda: list(ib.fills() or []))
    out = []
    for paper_id, con_id, qty, risk_money, status in rows:
        net = sum((f.commissionReport.realizedPNL or 0.0)
                  - (f.commissionReport.commission or 0.0)
                  for f in fills if getattr(f.contract, "conId", None) == con_id
                  and f.commissionReport)
        p = journal.get(paper_id, {})
        out.append({
            "paper_id": paper_id, "instrument": p.get("instrument", "?"),
            "ticket": con_id, "volume": qty, "closed": status == "CLOSED",
            "paper_status": p.get("status", "?"),
            "paper_r": p.get("realized_r", 0.0),
            "demo_r": (net / risk_money) if risk_money else 0.0,
            "demo_pnl": net,
        })
    return out


# ---- helpers ----------------------------------------------------------------

def _parse_expiry(ymd: str) -> dt.date | None:
    ymd = (ymd or "").strip()
    try:
        if len(ymd) >= 8:
            return dt.datetime.strptime(ymd[:8], "%Y%m%d").date()
        if len(ymd) >= 6:
            return dt.datetime.strptime(ymd[:6], "%Y%m").date().replace(day=28)
    except ValueError:
        return None
    return None


def main() -> None:
    if not ib_client.is_available():
        print("IB not available (TWS/Gateway running + API enabled?)."); return
    print(f"account={ib_client.account_id()}  paper={is_paper()}")
    rows = reconcile()
    if not rows:
        print("no mirrored trades yet."); return
    print(f"{'id':>4} {'instrument':<8} {'conId':>10} {'closed':>7} "
          f"{'paper':>8} {'paperR':>8} {'ibR':>8} {'pnl':>10}")
    for r in rows:
        print(f"{r['paper_id']:>4} {r['instrument']:<8} {r['ticket']:>10} "
              f"{str(r['closed']):>7} {r['paper_status']:>8} "
              f"{r['paper_r']:>8.2f} {r['demo_r']:>8.2f} {r['demo_pnl']:>10.2f}")


if __name__ == "__main__":
    main()
