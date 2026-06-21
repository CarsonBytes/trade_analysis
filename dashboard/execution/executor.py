"""Demo-account auto-execution: mirrors the paper journal to a real MT5 DEMO
terminal so forward trades get REAL broker fills, spreads and swaps.

HARD SAFETY: every entry point checks the connected account is a DEMO account
(broker-reported trade_mode) and refuses to do anything on a real one. This is
deliberate and non-configurable -- flipping a flag must never be enough to make
this code trade real money.

What it does, per refresh cycle (wired in service.py):
  - mirror_new():    place a market order (with SL/TP) for each newly OPEN
                     paper trade of the live variant (ATR rr3.0), sized to
                     RISK_PER_TRADE of demo equity via the broker's own
                     profit calculator. Failed sends (market closed, symbol
                     unknown) are simply retried next cycle.
  - sync_closures(): if the paper journal resolved a trade (e.g. EXPIRED at
                     horizon) while its MT5 position is still open, close it
                     at market. WIN/LOSS normally close server-side via SL/TP.
  - reconcile():     join MT5 deal history back onto the paper journal and
                     report realized R (real fills) vs paper R. This is the
                     analysis that tells you whether the paper cost model is
                     honest.  CLI: uv run python -m dashboard.executor
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401

import sqlite3
import datetime as dt

from dashboard.core import paper
from dashboard.data import mt5_client
from dashboard.instruments import BY_KEY
from dashboard.core.log import log

MIRROR_METHOD = "ATR rr3.0"   # the one live variant we execute
MAGIC = 990613                # tags our orders in the terminal
DEVIATION = 20                # max slippage, points


# ---- demo guard (non-negotiable) -------------------------------------------

def _mt5():
    """Connected MetaTrader5 module, or None."""
    if not mt5_client.is_available():
        return None
    return mt5_client._mod()


def is_demo() -> bool:
    """True only when the connected account is broker-flagged as DEMO."""
    mt5 = _mt5()
    if mt5 is None:
        return False
    with mt5_client._LOCK:
        info = mt5.account_info()
    return bool(info) and info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO


def _guard():
    """Return the mt5 module if and only if we're on a demo account."""
    mt5 = _mt5()
    if mt5 is None:
        return None
    if not is_demo():
        log.warning("executor: connected account is NOT demo -- refusing to trade")
        return None
    return mt5


# ---- mirror bookkeeping (same sqlite file as the journal) -------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(paper._DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS mt5_mirror (
        paper_id INTEGER UNIQUE, ticket INTEGER, volume REAL, risk_money REAL,
        ts TEXT, status TEXT, note TEXT)""")
    return c


def _mirrored_ids() -> set[int]:
    with paper._LOCK, _conn() as c:
        return {r[0] for r in c.execute("SELECT paper_id FROM mt5_mirror").fetchall()}


# ---- sizing ------------------------------------------------------------------

def _volume_for(mt5, symbol: str, direction: str, entry: float, sl: float,
                risk_money: float) -> float | None:
    """Lots such that hitting SL loses ~risk_money, using the broker's own
    calculator (handles contract size and quote-ccy conversion)."""
    otype = mt5.ORDER_TYPE_BUY if direction == "long" else mt5.ORDER_TYPE_SELL
    loss = mt5.order_calc_profit(otype, symbol, 1.0, entry, sl)
    if loss is None or loss >= 0:
        return None
    info = mt5.symbol_info(symbol)
    if info is None:
        return None
    vol = risk_money / abs(loss)
    step = info.volume_step or 0.01
    vol = max(info.volume_min, min(info.volume_max, round(vol / step) * step))
    return round(vol, 8)


# ---- actions -----------------------------------------------------------------

def mirror_new() -> list[str]:
    """Place demo orders for OPEN paper trades of the live variant that we
    haven't mirrored yet. Returns human-readable log lines."""
    mt5 = _guard()
    if mt5 is None:
        return []
    from dashboard.execution import link_monitor
    with link_monitor.ORDER_GATE:           # block link re-rolls while we trade
        return _mirror_new(mt5)


def _mirror_new(mt5) -> list[str]:
    done = _mirrored_ids()
    logs: list[str] = []
    with mt5_client._LOCK:
        equity = mt5.account_info().equity
    for t in paper.open_trades():
        if t["method"] != MIRROR_METHOD or t["id"] in done:
            continue
        inst = BY_KEY.get(t["instrument"])
        if inst is None:
            continue
        symbol = inst.mt5
        risk_money = equity * paper.RISK_PER_TRADE
        with mt5_client._LOCK:
            if not mt5_client._select(symbol):
                logs.append(f"{t['instrument']}: symbol {symbol} unknown to broker, skip")
                continue
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue
            vol = _volume_for(mt5, symbol, t["direction"], t["entry"], t["sl"], risk_money)
            if not vol:
                logs.append(f"{t['instrument']}: cannot size order, skip")
                continue
            buy = t["direction"] == "long"
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": vol,
                "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
                "price": tick.ask if buy else tick.bid,
                "sl": float(t["sl"]),
                "tp": float(t["tp"]),
                "deviation": DEVIATION,
                "magic": MAGIC,
                "comment": f"quant#{t['id']}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            res = mt5.order_send(req)
            if res is None or res.retcode not in (mt5.TRADE_RETCODE_DONE,
                                                  mt5.TRADE_RETCODE_DONE_PARTIAL):
                # unsupported filling mode? one retry with FOK
                if res is not None and res.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
                    req["type_filling"] = mt5.ORDER_FILLING_FOK
                    res = mt5.order_send(req)
        if res is None or res.retcode not in (mt5.TRADE_RETCODE_DONE,
                                              mt5.TRADE_RETCODE_DONE_PARTIAL):
            why = getattr(res, "comment", "no response")
            logs.append(f"{t['instrument']}: order REJECTED ({why}) -- will retry")
            log.warning("executor: %s order rejected: %s", symbol, why)
            continue  # not recorded -> retried next cycle
        with paper._LOCK, _conn() as c:
            c.execute("INSERT OR IGNORE INTO mt5_mirror VALUES (?,?,?,?,?,?,?)",
                      (t["id"], res.order, res.volume, risk_money,
                       dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       "OPEN", ""))
        msg = (f"{t['instrument']}: demo order FILLED ticket {res.order} "
               f"vol {res.volume} @ {res.price}")
        logs.append(msg)
        log.info("executor: %s", msg)
    return logs


def sync_closures() -> list[str]:
    """Close demo positions whose paper trade has resolved (e.g. EXPIRED).
    WIN/LOSS normally close server-side via SL/TP -- this catches the rest."""
    mt5 = _guard()
    if mt5 is None:
        return []
    from dashboard.execution import link_monitor
    with link_monitor.ORDER_GATE:           # block link re-rolls while we trade
        return _sync_closures(mt5)


def _sync_closures(mt5) -> list[str]:
    logs: list[str] = []
    with paper._LOCK, _conn() as c:
        rows = c.execute("SELECT paper_id, ticket, status FROM mt5_mirror").fetchall()
    journal = {t["id"]: t for t in paper.all_trades()}
    resolved = {tid: t for tid, t in journal.items() if t["status"] != "OPEN"}
    for paper_id, ticket, mstatus in rows:
        pt = journal.get(paper_id)
        if pt is None or (mstatus == "CLOSED" and pt["status"] != "OPEN"):
            continue  # fully reconciled (both closed) -- skip
        with mt5_client._LOCK:
            pos = mt5.positions_get(ticket=ticket)
        if not pos:  # MT5 position is gone (closed server-side or already closed)
            with paper._LOCK, _conn() as c:
                c.execute("UPDATE mt5_mirror SET status='CLOSED' WHERE paper_id=?",
                          (paper_id,))
            # GROUND TRUTH: if the paper trade is still OPEN, resolve it from the
            # broker's actual close (the tick re-derivation can disagree due to
            # SL/TP rounding, leaving the paper trade stuck open forever).
            if pt["status"] == "OPEN":
                msg = _resolve_from_broker(mt5, pt, ticket)
                if msg:
                    logs.append(msg); log.info("executor: %s", msg)
            continue
        if paper_id not in resolved:
            continue  # both still open -- nothing to do
        p = pos[0]
        buy = p.type == mt5.POSITION_TYPE_BUY
        with mt5_client._LOCK:
            tick = mt5.symbol_info_tick(p.symbol)
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                "position": ticket,
                "price": tick.bid if buy else tick.ask,
                "deviation": DEVIATION,
                "magic": MAGIC,
                "comment": f"quant#{paper_id} {resolved[paper_id]['status']}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            res = mt5.order_send(req)
        ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
        if ok:
            with paper._LOCK, _conn() as c:
                c.execute("UPDATE mt5_mirror SET status='CLOSED', note=? WHERE paper_id=?",
                          (f"closed on paper {resolved[paper_id]['status']}", paper_id))
        msg = (f"ticket {ticket}: {'closed' if ok else 'CLOSE FAILED'} "
               f"(paper says {resolved[paper_id]['status']})")
        logs.append(msg)
        log.info("executor: %s", msg)
    return logs


def _resolve_from_broker(mt5, trade: dict, ticket: int) -> str | None:
    """Resolve a paper trade from its mirrored MT5 position's CLOSING deal --
    the broker is ground truth for trades it executed. Returns a log line."""
    with mt5_client._LOCK:
        deals = mt5.history_deals_get(position=ticket) or []
    closeds = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
    if not closeds:
        return None
    d = closeds[-1]
    exit_price = float(d.price)
    r = paper.r_multiple(trade["direction"], trade["entry"], trade["sl"], exit_price,
                         half_spread=trade.get("half_spread") or paper.HALF_SPREAD)
    reason = {4: "stop-loss hit", 5: "take-profit hit"}.get(
        getattr(d, "reason", -1), "closed at broker")
    status = "WIN" if r > 0 else "LOSS"
    import pandas as pd
    offset = paper.store.cache_get("mt5_offset_sec")[0] or 0
    exit_ts = paper._mt5_to_utc(pd.Timestamp(d.time, unit="s", tz="UTC"), offset)
    paper._update_resolution(trade["id"], status, str(exit_ts), exit_price,
                             round(r, 3), exit_reason=f"{reason} (broker)")
    return (f"#{trade['id']} {trade['instrument']} resolved from BROKER: {status} "
            f"R={r:+.2f} exit={exit_price} ({reason})")


# ---- one-off maintenance: flatten positions that aren't ours ----------------

def _close_position(mt5, p) -> tuple[bool, str]:
    """Close a single open position at market. Returns (ok, broker_comment)."""
    buy = p.type == mt5.POSITION_TYPE_BUY
    with mt5_client._LOCK:
        mt5_client._select(p.symbol)
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            return False, "no tick"
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
            "position": p.ticket,
            "price": tick.bid if buy else tick.ask,
            "deviation": DEVIATION,
            "magic": MAGIC,
            "comment": "flatten foreign",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is not None and res.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
            req["type_filling"] = mt5.ORDER_FILLING_FOK
            res = mt5.order_send(req)
    if res is None:
        return False, "no response"
    ok = res.retcode == mt5.TRADE_RETCODE_DONE
    return ok, getattr(res, "comment", str(res.retcode))


def live_positions() -> dict:
    """Map paper_id -> live MT5 position {ticket, open, profit, volume, sl, tp,
    direction} for OUR trades (matched by the 'quant#<id>' order comment). Lets
    the UI show the REAL fill price + P&L instead of the paper entry."""
    mt5 = _mt5()
    if mt5 is None:
        return {}
    out: dict[int, dict] = {}
    with mt5_client._LOCK:
        for p in (mt5.positions_get() or []):
            if p.magic != MAGIC or not str(p.comment).startswith("quant#"):
                continue
            try:
                pid = int(str(p.comment).split("#")[1])
            except (ValueError, IndexError):
                continue
            out[pid] = {"ticket": p.ticket, "open": float(p.price_open),
                        "profit": float(p.profit), "volume": float(p.volume),
                        "sl": float(p.sl), "tp": float(p.tp),
                        "direction": "long" if p.type == mt5.POSITION_TYPE_BUY else "short"}
    return out


def foreign_positions() -> list:
    """Open positions NOT placed by our strategy (magic != MAGIC)."""
    mt5 = _mt5()
    if mt5 is None:
        return []
    with mt5_client._LOCK:
        return [p for p in (mt5.positions_get() or []) if p.magic != MAGIC]


def flatten_foreign(poll: bool = False, interval: int = 60,
                    timeout_h: float = 72.0) -> list[str]:
    """Close every open position that isn't ours. With poll=True, keep retrying
    on 'market closed' until the close goes through (or timeout_h elapses), so
    this can be launched while the market is shut and it fires on reopen.
    Demo-guarded: refuses to act on a non-demo account."""
    import time
    deadline = time.time() + timeout_h * 3600
    logs: list[str] = []
    while True:
        # safety + robustness: distinguish "MT5 not up yet" (retryable when
        # polling -- e.g. terminal launches after this task) from "live account"
        # (HARD abort, must never retry into a real-money trade).
        if not mt5_client.is_available():
            if poll and time.time() < deadline:
                log.info("flatten_foreign: MT5 terminal not available; retrying in %ds",
                         interval)
                time.sleep(interval); continue
            return ["aborted: MT5 terminal not available"]
        if not is_demo():
            log.warning("flatten_foreign: account is NOT demo -- refusing to trade")
            return ["aborted: not a demo account"]
        mt5 = _mt5()
        targets = foreign_positions()
        if not targets:
            logs.append("no foreign positions open -- account is flat")
            log.info("flatten_foreign: nothing to close")
            return logs
        remaining = []
        for p in targets:
            ok, why = _close_position(mt5, p)
            tag = (f"{p.symbol} {p.volume}lot ticket {p.ticket} "
                   f"(magic {p.magic}, '{p.comment}')")
            if ok:
                msg = f"CLOSED {tag}"
                logs.append(msg); log.info("flatten_foreign: %s", msg)
            else:
                remaining.append((p, why))
        if not remaining:
            return logs
        if not poll or time.time() > deadline:
            for p, why in remaining:
                msg = f"could NOT close ticket {p.ticket}: {why}"
                logs.append(msg); log.warning("flatten_foreign: %s", msg)
            return logs
        # market likely closed -- wait and retry
        log.info("flatten_foreign: %d position(s) unclosed (%s); retrying in %ds",
                 len(remaining), remaining[0][1], interval)
        time.sleep(interval)


# ---- analysis ----------------------------------------------------------------

def reconcile() -> list[dict]:
    """Per mirrored trade: paper R vs realized demo R from actual deal history
    (profit + swap + commission, normalised by the risk money at placement)."""
    mt5 = _mt5()
    if mt5 is None:
        return []
    with paper._LOCK, _conn() as c:
        rows = c.execute("SELECT paper_id, ticket, volume, risk_money, status "
                         "FROM mt5_mirror").fetchall()
    journal = {t["id"]: t for t in paper.all_trades()}
    out = []
    for paper_id, ticket, vol, risk_money, status in rows:
        with mt5_client._LOCK:
            deals = mt5.history_deals_get(position=ticket) or []
        net = sum(d.profit + d.swap + d.commission for d in deals)
        closed = status == "CLOSED" or (deals and not mt5.positions_get(ticket=ticket))
        p = journal.get(paper_id, {})
        out.append({
            "paper_id": paper_id, "instrument": p.get("instrument", "?"),
            "ticket": ticket, "volume": vol, "closed": closed,
            "paper_status": p.get("status", "?"),
            "paper_r": p.get("realized_r", 0.0),
            "demo_r": (net / risk_money) if risk_money else 0.0,
            "demo_pnl": net,
        })
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--flatten-foreign", action="store_true",
                    help="close every position not placed by our strategy")
    ap.add_argument("--wait-open", action="store_true",
                    help="with --flatten-foreign: poll until market opens, then close")
    args = ap.parse_args()

    if not mt5_client.is_available():
        print("MT5 terminal not available."); return
    print(f"account is demo: {is_demo()}")

    if args.flatten_foreign:
        targets = foreign_positions()
        print(f"foreign positions to close: {len(targets)}")
        for p in targets:
            print(f"  {p.symbol} {p.volume}lot ticket {p.ticket} "
                  f"magic {p.magic} '{p.comment}'")
        for line in flatten_foreign(poll=args.wait_open):
            print(" ", line)
        return

    rows = reconcile()
    if not rows:
        print("no mirrored trades yet."); return
    print(f"{'id':>4} {'instrument':<10} {'ticket':>10} {'closed':>7} "
          f"{'paper':>8} {'paperR':>8} {'demoR':>8} {'pnl':>10}")
    for r in rows:
        print(f"{r['paper_id']:>4} {r['instrument']:<10} {r['ticket']:>10} "
              f"{str(r['closed']):>7} {r['paper_status']:>8} "
              f"{r['paper_r']:>8.2f} {r['demo_r']:>8.2f} {r['demo_pnl']:>10.2f}")
    done = [r for r in rows if r["closed"] and r["paper_status"] not in ("OPEN", "?")]
    if done:
        gap = sum(r["demo_r"] - r["paper_r"] for r in done) / len(done)
        print(f"\navg demo-vs-paper R gap on {len(done)} closed trades: {gap:+.3f} "
              f"(negative = paper cost model too optimistic)")


if __name__ == "__main__":
    main()
