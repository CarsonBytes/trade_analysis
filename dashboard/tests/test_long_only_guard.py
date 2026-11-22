"""Long-only guard (IBKR_LONG_ONLY_GUARD_SPEC, 2026-11-21).
Phase 1 (G6.1 / G8):
  - scripts/flex_nav_reconcile.py: StockShort parsing + short_alarm() (CSV and XML).
  - ib_client.portfolio_item_dict(): PortfolioItem -> plain dict for the untracked card.
Phase 2 (G1 / G4), spec tests T1-T3 and T6-T8:
  - T1 place_guarded() is the only caller of placeOrder (AST scan of dashboard/).
  - T2 the guard refuses/clamps SELLs beyond the long; OCA legs count once; covers can't flip.
  - T3 the guard fails closed when the broker read fails.
  - T6 the QQQ 2026-10-30 replay: reprotect never arms SELL against a short broker position.
  - T7 reprotect waits out the 60s settle window after a fill/cancel.
  - T8 a heal 8 -> 7 resizes the resting legs to 7 (or cancels them if it can't).
No network, no broker (fakes only).
Run:  uv run python -m dashboard.tests.test_long_only_guard
"""
from __future__ import annotations

from dashboard.tests import hermetic  # noqa: F401  (before any app module: no real pushes)

import ast
import os
import datetime as dt
import importlib.util
import pathlib
from types import SimpleNamespace
from unittest import mock

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def _flex():
    p = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "flex_nav_reconcile.py"
    spec = importlib.util.spec_from_file_location("flex_nav_reconcile", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Shape of the real LIVE CSV statement (query 1659726), trimmed to the NAV section.
_CSV = b'''"BOF","U0000000","StatementOfFundsNetLiquidation","4","20261116","20261120","20261121;000000","100","100"
"BOA","Acct",20261116,20261120
"BOS","EQUT","Net Asset Value (NAV) in Base; trade date basis"
"HEADER","EQUT","ClientAccountID","ReportDate","Cash","Stock","StockLong","StockShort","Total"
"DATA","EQUT","U0000000","20261116","70996.26","211338.02","284890.66","-73552.63","282155.00"
"DATA","EQUT","U0000000","20261119","75453.96","209730.53","303012.21","-93281.67","285167.63"
"DATA","EQUT","U0000000","20261120","75412.85","213485.88","302936.38","-89450.51","288872.63"
"EOS","EQUT"
"EOA","Acct"
"EOF"
'''


def test_flex_short_stock_csv():
    print("flex: StockShort parsed per day from the CSV NAV section (the 2026-11 LIVE book):")
    f = _flex()
    by_day = f._short_stock_by_day(_CSV)
    check("three days parsed", len(by_day), 3)
    check("Nov 20 short value", by_day[dt.date(2026, 11, 20)], -89450.51)
    check("NAV still parses from the same bytes",
          f._parse_nav(_CSV)[dt.date(2026, 11, 20)], 288872.63)
    check("alarm on the latest day", f.short_alarm(by_day), (dt.date(2026, 11, 20), -89450.51))


def test_flex_short_alarm_rules():
    print("\nflex short_alarm(): latest day only, tolerance respected, no data -> no alarm:")
    f = _flex()
    d1, d2 = dt.date(2026, 11, 19), dt.date(2026, 11, 20)
    check("covered by the latest day -> history, not an alarm",
          f.short_alarm({d1: -93281.67, d2: 0.0}), None)
    check("rounding noise is not a short", f.short_alarm({d2: -0.4}), None)
    check("empty -> None", f.short_alarm({}), None)
    flat = _CSV.replace(b'"-89450.51"', b'"0"')
    check("flat latest day in CSV -> None", f.short_alarm(f._short_stock_by_day(flat)), None)


def test_flex_short_stock_xml():
    print("\nflex: StockShort from the XML statement shape:")
    f = _flex()
    xml = (b'<FlexQueryResponse><FlexStatements><FlexStatement>'
           b'<EquitySummaryInBase>'
           b'<EquitySummaryInReport ReportDate="2026-11-20" Cash="1" Total="288872.63" '
           b'StockShort="-89450.51"/>'
           b'</EquitySummaryInBase></FlexStatement></FlexStatements></FlexQueryResponse>')
    check("xml short parsed", f._short_stock_by_day(xml), {dt.date(2026, 11, 20): -89450.51})
    check("xml without the column -> no data (not 'flat')",
          f._short_stock_by_day(xml.replace(b' StockShort="-89450.51"', b"")), {})


def test_portfolio_item_dict():
    print("\nib_client.portfolio_item_dict(): PortfolioItem -> JSON-safe dict:")
    from dashboard.data.ib_client import portfolio_item_dict
    item = SimpleNamespace(
        contract=SimpleNamespace(symbol="QQQ", conId=320227571, secType="STK", currency="USD"),
        position=-8.0, averageCost=692.94, marketPrice=640.0, marketValue=-5120.0,
        unrealizedPNL=423.5)
    check("short row", portfolio_item_dict(item),
          {"symbol": "QQQ", "con_id": 320227571, "qty": -8.0, "avg_cost": 692.94,
           "market_price": 640.0, "market_value": -5120.0, "unrealized_pnl": 423.5,
           "currency": "USD"})
    cash = SimpleNamespace(contract=SimpleNamespace(symbol="USD", conId=1, secType="CASH"),
                           position=9558.0)
    check("CASH row dropped", portfolio_item_dict(cash), None)
    flat = SimpleNamespace(contract=SimpleNamespace(symbol="EEM", conId=2, secType="STK"),
                           position=0.0)
    check("flat row dropped", portfolio_item_dict(flat), None)


# ============================== phase 2: G1 / G4 ==============================

def _tr(con, action, qty, *, ref="", oca="", parent=0, oid=1, cid=31, perm=0,
        otype="STP", remaining=None):
    """A fake ib_async Trade as reqAllOpenOrdersAsync()/openTrades() return it."""
    order = SimpleNamespace(action=action, totalQuantity=qty, orderRef=ref, ocaGroup=oca,
                            parentId=parent, orderId=oid, clientId=cid, permId=perm,
                            orderType=otype)
    st = SimpleNamespace(remaining=qty if remaining is None else remaining, filled=0.0)
    return SimpleNamespace(order=order, contract=SimpleNamespace(conId=con, symbol="X"),
                           orderStatus=st)


def test_t1_only_place_guarded_calls_placeOrder():
    print("\nT1: place_guarded() is the ONLY code that calls .placeOrder() (AST scan of "
          "dashboard/, tests excluded) -- one choke point, so no path can skip the guard:")
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts or ".venv" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == "place_guarded" \
                    and path.name == "ib_exec.py":
                allowed |= {id(n) for n in ast.walk(fn)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "placeOrder" and id(n) not in allowed:
                offenders.append(f"{path.relative_to(root)}:{n.lineno}")
    check("no direct placeOrder outside place_guarded", offenders, [])


def test_t2_guard_refuses_sell_beyond_long():
    print("\nT2: guard_check() -- a SELL may never exceed (broker long - resting SELLs):")
    from dashboard.execution.ib_exec import guard_check as g
    check("long 7, resting SELL 8 -> new SELL 1 refused",
          g("SELL", 1, "exit", position=7, resting_sell=8)[0], "refuse")
    check("long 0 -> SELL 7 refused (would open a short)",
          g("SELL", 7, "reprotect", position=0, resting_sell=0)[0], "refuse")
    check("broker SHORT -1 -> SELL 7 refused (the QQQ Oct 30 step)",
          g("SELL", 7, "reprotect", position=-1, resting_sell=0)[0], "refuse")
    check("long 7, nothing resting -> SELL 7 ok",
          g("SELL", 7, "exit", position=7, resting_sell=0)[:2], ("ok", 7.0))
    check("exit over the limit is CLAMPED to what is held",
          g("SELL", 8, "exit", position=7, resting_sell=0)[:2], ("clamp", 7.0))
    check("a sweep/withdraw SELL over the limit is REFUSED, not clamped",
          g("SELL", 8, "sweep", position=7, resting_sell=0)[0], "refuse")
    check("an entry SELL (a short entry) is refused on a flat book",
          g("SELL", 5, "entry", position=0, resting_sell=0)[0], "refuse")
    check("bracket child of an unfilled parent BUY is exempt",
          g("SELL", 5, "entry", position=0, resting_sell=0,
            attached_to_unfilled_parent=True)[0], "ok")
    check("BUY entries are not I1-checked", g("BUY", 5, "entry")[0], "ok")
    check("FX (CASH) conversions pass", g("BUY", 1000, "fx", sec_type="CASH")[0], "ok")
    check("unknown intent refused", g("SELL", 1, "yolo", position=9, resting_sell=0)[0],
          "refuse")
    print("  cover BUYs can never flip a short into a long (2026-09-29 HYD overshoot):")
    check("short -20, BUY 66 cover -> clamped to 20",
          g("BUY", 66, "cover", position=-20)[:2], ("clamp", 20.0))
    check("short -20 with 15 already resting to cover -> only 5 more",
          g("BUY", 20, "cover", position=-20, resting_buy=15)[:2], ("clamp", 5.0))
    check("nothing short -> cover refused", g("BUY", 5, "cover", position=3)[0], "refuse")

    print("  resting_qty_by_con(): OCA/bracket legs count once, inactive children skipped:")
    from dashboard.execution.ib_exec import resting_qty_by_con as r
    oca_pair = [_tr(7, "SELL", 8, oca="reprotect5", oid=1), _tr(7, "SELL", 8, oca="reprotect5", oid=2)]
    check("TP 8 + SL 8 in one OCA = 8 resting, not 16", r(oca_pair)[7]["SELL"], 8.0)
    filled_parent_kids = [_tr(7, "SELL", 8, parent=100, oid=101), _tr(7, "SELL", 8, parent=100, oid=102)]
    check("children of a FILLED parent: 8 resting (siblings one-cancels-all)",
          r(filled_parent_kids)[7]["SELL"], 8.0)
    live_parent = filled_parent_kids + [_tr(7, "BUY", 8, oid=100, otype="MKT")]
    check("children of a parent still RESTING are inactive -> 0 SELL",
          r(live_parent)[7]["SELL"], 0.0)
    orphan = [_tr(9, "SELL", 21, cid=44, oid=5, perm=777)]
    check("an orphan stop from ANOTHER client counts (the AMLP #16 class)", r(orphan)[9]["SELL"],
          21.0)
    check("excluded keys (orders being replaced) don't count",
          r(orphan, exclude={("perm", 777)}).get(9, {}).get("SELL", 0.0), 0.0)


def test_t2_place_guarded_clamps_and_refuses_on_the_wire():
    print("\nT2b: place_guarded() is what actually reaches the broker -- clamp mutates the "
          "order, a refusal sends nothing and queues a RED event:")
    from dashboard.execution import ib_exec
    sent = []
    ib = SimpleNamespace(placeOrder=lambda c, o: sent.append((o.action, o.totalQuantity)) or "T")
    c = SimpleNamespace(conId=7, symbol="QQQ", secType="STK")
    snap = ib_exec.make_snapshot({7: 7.0}, [])
    o = SimpleNamespace(action="SELL", totalQuantity=8, ocaGroup="", parentId=0, orderId=0,
                        clientId=0, permId=0)
    check("exit 8 vs long 7 -> sent as 7", (ib_exec.place_guarded(ib, c, o, intent="exit",
                                                                  snap=snap), sent[-1]),
          ("T", ("SELL", 7)))
    ib_exec.GUARD_EVENTS.clear()
    o2 = SimpleNamespace(action="SELL", totalQuantity=1, ocaGroup="", parentId=0, orderId=0,
                         clientId=0, permId=0)
    n = len(sent)
    check("a second SELL now sees the first as resting -> refused",
          ib_exec.place_guarded(ib, c, o2, intent="exit", snap=snap), None)
    check("nothing sent for the refusal", len(sent), n)
    check("refusal queued as a GUARD event", len(ib_exec.GUARD_EVENTS), 1)
    with mock.patch("dashboard.core.notable_events.record") as rec, \
         mock.patch.object(ib_exec, "record_order_exec"):
        msgs = ib_exec.flush_guard_events()
    check("flush records it RED (level=error, kind=order-guard)",
          (len(msgs), rec.call_args.kwargs.get("level"), rec.call_args.kwargs.get("kind")),
          (1, "error", "order-guard"))


def test_t3_guard_fails_closed_on_failed_read():
    print("\nT3: when the broker picture is unknown, every SELL is refused (fails closed):")
    from dashboard.execution import ib_exec
    from dashboard.execution.ib_exec import guard_check as g
    check("unknown position -> refuse", g("SELL", 1, "exit", position=None, resting_sell=0)[0],
          "refuse")
    check("unknown open orders -> refuse", g("SELL", 1, "exit", position=9, resting_sell=None)[0],
          "refuse")
    with mock.patch.object(ib_exec.ib_client, "_run", side_effect=RuntimeError("gateway down")):
        snap = ib_exec.order_book_snapshot(object(), "U1")
    check("a failed read gives no snapshot", snap, None)
    sent = []
    ib = SimpleNamespace(placeOrder=lambda c, o: sent.append(o) or "T")
    c = SimpleNamespace(conId=7, symbol="QQQ", secType="STK")
    o = SimpleNamespace(action="SELL", totalQuantity=7, ocaGroup="", parentId=0)
    ib_exec.GUARD_EVENTS.clear()
    check("SELL with no snapshot -> refused, nothing sent",
          (ib_exec.place_guarded(ib, c, o, intent="exit", snap=None), sent), (None, []))
    ib_exec.GUARD_EVENTS.clear()

    print("  an EMPTY order read while a long is held is re-read before it is believed:")
    reads = iter([[], [_tr(7, "SELL", 7, oid=9)]])

    def _run(coro, timeout=None):
        coro.close()
        return next(reads)
    fake_ib = SimpleNamespace(positions=lambda: [SimpleNamespace(
        contract=SimpleNamespace(conId=7), position=7.0)])
    with mock.patch.object(ib_exec.ib_client, "_run", side_effect=_run), \
         mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, timeout=None: fn()), \
         mock.patch.object(ib_exec.ib_client, "filter_by_account", side_effect=lambda i, a: i), \
         mock.patch.object(ib_exec, "GUARD_EMPTY_REREAD_SEC", 0):
        snap = ib_exec.order_book_snapshot(fake_ib, "U1")
    check("second read's resting stop is what the guard sees",
          ib_exec.resting_qty_by_con(snap["orders"])[7]["SELL"], 7.0)


def _reprotect_run(broker_pos, *, settle_age_sec=None, direction="long"):
    """Drive reprotect_naked_positions() for one naked VNQ row (price inside its band) against
    a broker holding `broker_pos`. Returns (orders_placed, red_events, logs)."""
    from dashboard.execution import ib_exec
    con_id = 555
    rows = [(9, con_id, 7.0, "QQQ", direction, 90.0, 120.0, "OPEN")]
    pf = SimpleNamespace(contract=SimpleNamespace(conId=con_id), marketPrice=100.0,
                         position=broker_pos)
    placed, reds = [], []
    fake_ib = SimpleNamespace(placeOrder=lambda c, o: placed.append((o.action, o.totalQuantity))
                              or SimpleNamespace(order=o, orderStatus=SimpleNamespace(
                                  status="Submitted", filled=0.0, message="")))
    now = dt.datetime.now(dt.timezone.utc)
    events = ({} if settle_age_sec is None
              else {con_id: now - dt.timedelta(seconds=settle_age_sec)})
    n_calls = []

    def _call(fn, timeout=None):
        """1st ib_client.call = reprotect's (open conIds, portfolio) read; later = order sends."""
        n_calls.append(1)
        return ({2}, {con_id: pf}) if len(n_calls) == 1 else fn()
    with mock.patch.object(ib_exec, "_guard", return_value=fake_ib), \
         mock.patch.object(ib_exec.ib_client, "account_id", return_value="U1"), \
         mock.patch.object(ib_exec.ib_client, "_run", return_value=[_tr(2, "SELL", 1)]), \
         mock.patch.object(ib_exec.ib_client, "call", side_effect=_call), \
         mock.patch.object(ib_exec, "_recent_order_events", return_value=events), \
         mock.patch.object(ib_exec, "_stock_contract_for",
                           return_value=SimpleNamespace(conId=con_id, symbol="QQQ",
                                                        secType="STK")), \
         mock.patch.object(ib_exec, "manual_close_position",
                           side_effect=AssertionError("must not close")), \
         mock.patch.object(ib_exec, "confirm_order",
                           return_value={"ok": True, "status": "Submitted", "order_id": 1,
                                         "message": ""}), \
         mock.patch.object(ib_exec, "record_order_exec"), \
         mock.patch.object(ib_exec, "_conn"), mock.patch.object(ib_exec.paper, "_LOCK"), \
         mock.patch("dashboard.core.store.cache_get", return_value=(0, 0)), \
         mock.patch("dashboard.core.store.cache_set"), \
         mock.patch("dashboard.core.notable_events.record",
                    side_effect=lambda m, **k: reds.append(m) if k.get("level") == "error"
                    else None):
        ib_exec._conn.return_value.__enter__.return_value.execute.return_value \
            .fetchall.return_value = rows
        logs = ib_exec.reprotect_naked_positions()
    return placed, reds, logs


def test_t6_reprotect_refuses_when_broker_short():
    print("\nT6: the QQQ 2026-10-30 replay -- journal long 7, broker -1 after the stop "
          "over-filled. Reprotect used to arm SELL 7 on top (-> -8). Now: nothing placed, RED:")
    placed, reds, logs = _reprotect_run(-1.0)
    check("no order placed against a short broker position", placed, [])
    check("one RED event raised", len(reds), 1)
    check("event names the unintended short", "UNINTENDED SHORT" in (reds[0] if reds else ""),
          True)
    placed, reds, _ = _reprotect_run(0.0)
    check("broker flat -> nothing placed, no RED (sync_closures' case)", (placed, reds), ([], []))
    placed, reds, _ = _reprotect_run(7.0)
    check("broker long 7 -> the TP/SL pair is re-armed for 7",
          placed, [("SELL", 7), ("SELL", 7)])
    placed, _r, _l = _reprotect_run(5.0)
    check("broker long 5 (mirror says 7) -> legs CLAMPED to 5", placed, [("SELL", 5), ("SELL", 5)])

    print("  unit-level replay of the whole Oct 30 sequence ends FLAT, not -8:")
    from dashboard.execution.ib_exec import guard_check as g
    pos = 7.0
    stop = 7.0          # G4.3: heal 8 -> 7 resized the stop to 7 (it was left at 8)
    pos -= stop         # the stop fills
    rearm = g("SELL", 7, "reprotect", position=pos, resting_sell=0)
    check("re-arm against the now-flat position is refused", rearm[0], "refuse")
    check("final position", pos, 0.0)


def test_t7_reprotect_settle_delay():
    print("\nT7: no reprotect within 60s of a fill/cancel on the same contract (the QQQ re-arm "
          "fired 4s after its stop filled):")
    from dashboard.execution.ib_exec import settle_blocked, REPROTECT_SETTLE_SEC
    now = dt.datetime(2026, 10, 30, 14, 0, 4, tzinfo=dt.timezone.utc)
    check("fill 4s ago -> blocked", settle_blocked(now - dt.timedelta(seconds=4), now), True)
    check("fill 61s ago -> clear", settle_blocked(now - dt.timedelta(seconds=61), now), False)
    check("no known event -> clear", settle_blocked(None, now), False)
    check("naive timestamps are treated as UTC",
          settle_blocked((now - dt.timedelta(seconds=4)).replace(tzinfo=None), now), True)
    check("window is 60s", REPROTECT_SETTLE_SEC, 60)
    placed, _r, _l = _reprotect_run(7.0, settle_age_sec=4)
    check("end to end: a fill 4s ago -> nothing re-armed this cycle", placed, [])
    placed, _r, _l = _reprotect_run(7.0, settle_age_sec=90)
    check("end to end: 90s later -> re-armed", len(placed), 2)


def test_t8_qty_heal_resizes_resting_legs():
    print("\nT8: heal 8 -> 7 must bring the resting TP/SL legs to 7 (RC2: QQQ's stop stayed at "
          "8 against 7 shares):")
    from dashboard.execution import ib_exec
    legs = [_tr(7, "SELL", 8, ref="quant#42", parent=100, oid=101, otype="LMT"),
            _tr(7, "SELL", 8, ref="quant#42", parent=100, oid=102, otype="STP")]
    other = _tr(7, "SELL", 3, ref="quant#43", oid=200)
    check("legs_to_resize picks this trade's two legs, not trade #43's",
          [t.order.orderId for t in ib_exec.legs_to_resize(legs + [other], 7, 42, 7.0)],
          [101, 102])
    check("already the right size -> nothing", ib_exec.legs_to_resize(legs, 7, 42, 8.0), [])

    def _run_resize(open_trades, snapshot_orders, pos=7.0):
        sent, cancelled = [], []
        ib = SimpleNamespace(
            openTrades=lambda: open_trades,
            placeOrder=lambda c, o: sent.append((o.orderId, o.totalQuantity)) or "T",
            cancelOrder=lambda o: cancelled.append(o.orderId))
        with mock.patch.object(ib_exec, "order_book_snapshot",
                               return_value=ib_exec.make_snapshot({7: pos}, snapshot_orders)), \
             mock.patch.object(ib_exec.ib_client, "call",
                               side_effect=lambda fn, timeout=None: fn()), \
             mock.patch("dashboard.core.notable_events.record") as rec, \
             mock.patch.object(ib_exec, "record_order_exec"):
            msg = ib_exec._resize_resting_legs(ib, "U1", 7, 42, 7.0, "QQQ")
        return sent, cancelled, msg, rec

    legs = [_tr(7, "SELL", 8, ref="quant#42", parent=100, oid=101, otype="LMT"),
            _tr(7, "SELL", 8, ref="quant#42", parent=100, oid=102, otype="STP")]
    sent, cancelled, msg, _rec = _run_resize(legs, legs)
    check("both legs modified in place to 7 (same orderIds)", sent, [(101, 7), (102, 7)])
    check("nothing cancelled", cancelled, [])
    check("reported as resized", "resized 2" in (msg or ""), True)

    print("  a leg another client owns can't be modified -> cancel ours, RED, reprotect re-arms:")
    mine = _tr(7, "SELL", 8, ref="quant#42", parent=100, oid=101, otype="LMT")
    theirs = _tr(7, "SELL", 8, ref="quant#42", parent=100, oid=102, cid=44, perm=999,
                 otype="STP")
    sent, cancelled, msg, rec = _run_resize([mine], [mine, theirs])
    check("nothing modified", sent, [])
    check("our leg cancelled", cancelled, [101])
    check("RED event raised", rec.call_args.kwargs.get("level"), "error")


# ============================== phase 3: G2 / G3 / G3.4 ==============================

def test_t4_void_requires_confirmed_cancel():
    print("\nT4: cancel_pending_step() -- VOID only after two clean reads >= 30s apart; still "
          "resting after 3 cycles -> RED once:")
    from dashboard.execution.ib_exec import cancel_pending_step as step
    st, a = step(None, present=True, now_ts=0)
    check("order still listed -> re-cancel, not VOID", a, "recancel")
    st, a = step(st, present=True, now_ts=10)
    st, a = step(st, present=True, now_ts=20)
    check("3rd cycle still resting -> escalate (RED)", a, "escalate")
    st, a = step(st, present=True, now_ts=30)
    check("...but only once", a, "recancel")
    st, a = step(st, present=False, now_ts=40)
    check("first clean read -> wait (a post-reconnect empty list is not proof)", a, "wait")
    st, a = step(st, present=False, now_ts=50)
    check("second clean read only 10s later -> still wait", a, "wait")
    st, a = step(st, present=False, now_ts=75)
    check("second clean read >= 30s after the first -> VOID", a, "void")
    st, a = step({"absent": 1, "last": 0.0, "cycles": 0, "alerted": False}, True, 100)
    check("the order reappearing resets the clean-read count", st["absent"], 0)


def test_t5_orphan_stop_after_void_is_classified_and_cancelled():
    print("\nT5: the AMLP #16 replay -- a VOID-era SELL STP 21 with no long under it is an "
          "ORPHAN_SELL and gets cancelled; covered/foreign orders are left alone:")
    from dashboard.execution import ib_exec
    AMLP, QQQ, SGOV = 9, 7, 5
    amlp_stop = _tr(AMLP, "SELL", 21, ref="quant#16", oid=31, otype="STP")
    rows = ib_exec.classify_open_orders([amlp_stop], {16: "VOID"}, {AMLP: 0.0})
    check("VOID trade's stop, broker flat -> ORPHAN_SELL", rows[0]["class"], "ORPHAN_SELL")
    rows = ib_exec.classify_open_orders([amlp_stop], {16: "VOID"}, {AMLP: 21.0})
    check("same stop but a real untracked long covers it -> ORPHAN_SELL_COVERED (left alone)",
          rows[0]["class"], "ORPHAN_SELL_COVERED")
    mixed = [_tr(QQQ, "SELL", 7, ref="quant#79", oid=41, otype="STP"),
             _tr(QQQ, "SELL", 7, ref="quant#80", oid=42, otype="STP"),
             _tr(QQQ, "SELL", 1, ref="", oid=43, otype="STP"),
             _tr(SGOV, "BUY", 50, ref="cash-sweep-sgov", oid=44, otype="MKT"),
             _tr(QQQ, "BUY", 5, ref="quant#81", oid=45, otype="MKT")]
    rows = ib_exec.classify_open_orders(mixed, {79: "OPEN", 80: "CANCEL_PENDING", 81: "VOID"},
                                        {QQQ: 7.0})
    check("classes", [r["class"] for r in rows],
          ["OWNED", "CANCELLING", "FOREIGN", "SHIELD", "ORPHAN_BUY"])
    # a second app-owned stop for a RESOLVED trade on top of an owned one = the QQQ 7 -> -8 shape
    rows = ib_exec.classify_open_orders(
        [_tr(QQQ, "SELL", 7, ref="quant#79", oid=41), _tr(QQQ, "SELL", 8, ref="quant#70", oid=46)],
        {79: "OPEN", 70: "LOSS"}, {QQQ: 7.0})
    check("resolved trade's extra stop over an owned one -> ORPHAN_SELL", rows[1]["class"],
          "ORPHAN_SELL")

    print("  sweep_orphan_orders(): act mode cancels, report mode doesn't, RED after 3 cycles:")
    from dashboard.core import store as _store
    mem = {}
    cancelled, reds = [], []
    ib = SimpleNamespace(cancelOrder=lambda o: cancelled.append(o.orderId))
    conn = mock.MagicMock()
    conn.__enter__.return_value.execute.return_value.fetchall.return_value = [(16, "VOID")]
    with mock.patch.object(ib_exec, "_conn", return_value=conn), \
         mock.patch.object(ib_exec.paper, "_LOCK", mock.MagicMock()), \
         mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, timeout=None: fn()), \
         mock.patch.object(_store, "cache_get", side_effect=lambda k: (mem.get(k), None)), \
         mock.patch.object(_store, "cache_set", side_effect=lambda k, v: mem.__setitem__(k, v)), \
         mock.patch("dashboard.core.notable_events.record",
                    side_effect=lambda m, **k: reds.append(m) if k.get("level") == "error" else None):
        with mock.patch.dict(os.environ, {"ORPHAN_SWEEP": "report"}):
            ib_exec.sweep_orphan_orders(ib, "U1", [amlp_stop], {AMLP: 0.0})
        check("report mode: nothing cancelled", cancelled, [])
        mem.clear(); reds.clear()
        with mock.patch.dict(os.environ, {"ORPHAN_SWEEP": "act"}):
            for _ in range(3):
                ib_exec.sweep_orphan_orders(ib, "U1", [amlp_stop], {AMLP: 0.0})
        check("act mode: cancel sent every cycle it still rests", cancelled, [31, 31, 31])
        check("RED on first sighting + RED 'cannot be cancelled' after 3 cycles", len(reds), 2)
        check("the escalation names the ops tool", "global_cancel_rearm" in reds[-1], True)


def _fill(ref, side, shares, price, con=9, sym="AMLP", exec_id="e1", when=None):
    ex = SimpleNamespace(orderRef=ref, side=side, shares=shares, price=price, execId=exec_id,
                         time=when or dt.datetime(2026, 7, 13, 14, 0, tzinfo=dt.timezone.utc))
    return SimpleNamespace(execution=ex, contract=SimpleNamespace(conId=con, symbol=sym))


def test_t16_void_entry_that_filled_is_caught():
    print("\nT16: G3.4 fill audit -- the journal called AMLP #16 VOID, but the broker filled "
          "BUY 2+7+12 @ 53.15 on 2026-07-13:")
    from dashboard.execution import ib_exec
    fills = [_fill("quant#16", "BOT", 2, 53.15, exec_id="a"),
             _fill("quant#16", "BOT", 7, 53.15, exec_id="b"),
             _fill("quant#16", "BOT", 12, 53.15, exec_id="c"),
             _fill("quant#16", "SLD", 21, 51.10, exec_id="d"),          # later exit: ignored
             _fill("quant#12", "BOT", 84, 38.37, con=11, sym="CPER", exec_id="e"),
             _fill("quant#79", "BOT", 7, 717.67, con=7, sym="QQQ", exec_id="f")]
    rows = {16: ("VOID", "order cancelled ...", 9),
            12: ("CLOSED", "entry never filled -- ghost, auto-cancelled", 11),
            79: ("CLOSED", "", 7)}                                       # a real, normal close
    found = {f["paper_id"]: (f["qty"], round(f["avg_px"], 2)) for f in
             ib_exec.void_fill_findings(fills, rows)}
    check("VOID #16 -> one finding, 21 shares @ 53.15", found.get(16), (21.0, 53.15))
    check("ghost-CLOSED #12 -> found too", found.get(12), (84.0, 38.37))
    check("a normally closed trade is not a finding", 79 in found, False)

    print("  audit_void_fills(): RED once, and re-open the row if the broker still holds it:")
    from dashboard.core import store as _store
    mem, reds, sql = {}, [], []
    conn = mock.MagicMock()
    inner = conn.__enter__.return_value

    def _exec(q, args=()):
        sql.append((q, args))
        r = mock.MagicMock()
        if q.startswith("SELECT paper_id, status, note, con_id"):
            r.__iter__ = lambda self: iter([(16, "VOID", "order cancelled", 9)])
        elif q.startswith("SELECT 1 FROM ib_mirror"):
            r.fetchone.return_value = None
        return r
    inner.execute.side_effect = _exec
    with mock.patch.object(ib_exec, "_conn", return_value=conn), \
         mock.patch.object(ib_exec.paper, "_LOCK", mock.MagicMock()), \
         mock.patch.object(ib_exec, "_broker_fills", return_value=fills[:3]), \
         mock.patch.object(_store, "cache_get", side_effect=lambda k: (mem.get(k), None)), \
         mock.patch.object(_store, "cache_set", side_effect=lambda k, v: mem.__setitem__(k, v)), \
         mock.patch("dashboard.core.notable_events.record",
                    side_effect=lambda m, **k: reds.append(m)):
        logs = ib_exec.audit_void_fills(object(), {9: 21.0}, force=True)
        again = ib_exec.audit_void_fills(object(), {9: 21.0}, force=True)
    check("one RED finding", len(reds), 1)
    check("names the real fill", "FILLED BUY 21" in (logs[0] if logs else ""), True)
    check("row re-opened (broker still holds 21, nothing tracks it)",
          any(q.startswith("UPDATE ib_mirror SET status='OPEN'") for q, _a in sql), True)
    check("already-reported fills are not re-raised", again, [])


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except AssertionError:
                pass
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
