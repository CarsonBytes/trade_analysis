"""Unit tests for the PURE sanity-guard functions in web/service.py -- equity-history
self-heal and the account-summary confirm-then-accept guard. Run:
  uv run python -m dashboard.tests.test_service
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
from unittest import mock

from dashboard.web.service import (heal_series, is_nl_implausible, pending_confirms,
                                   is_equity_jump_implausible, reconcile_due,
                                   hist_cash_gpv, detect_external_cash_flow)

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def approx(name, got, want, tol=1e-6):
    ok = abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want ~{want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want ~{want!r}"


def test_heal_series_bracketed_zero_spike():
    print("heal_series: bracketed zero-spike (the 2026-07-10 incident shape):")
    hist = [["t1", 100.0, "HKD"], ["t2", 0.0, "HKD"], ["t3", 0.0, "HKD"],
            ["t4", 101.0, "HKD"]]
    cleaned, removed = heal_series(hist)
    check("cleaned drops the spike", cleaned, [["t1", 100.0, "HKD"], ["t4", 101.0, "HKD"]])
    check("removed captures the spike", removed, [["t2", 0.0, "HKD"], ["t3", 0.0, "HKD"]])


def test_heal_series_real_sustained_jump_kept():
    print("heal_series: real sustained jump (e.g. a genuine deposit) is kept untouched:")
    hist = [["t1", 100.0, "HKD"], ["t2", 500.0, "HKD"], ["t3", 505.0, "HKD"],
            ["t4", 510.0, "HKD"]]
    cleaned, removed = heal_series(hist)
    check("nothing removed", removed, [])
    check("all points kept", cleaned, hist)


def test_heal_series_unresolved_anomaly_left_alone():
    print("heal_series: an anomaly still at the end of the series (unconfirmed) is left alone:")
    hist = [["t1", 100.0, "HKD"], ["t2", 101.0, "HKD"], ["t3", 0.0, "HKD"]]
    cleaned, removed = heal_series(hist)
    check("nothing removed (not yet bracketed)", removed, [])
    check("cleaned == original", cleaned, hist)


def test_heal_series_normal_fluctuations_untouched():
    print("heal_series: normal small fluctuations never trigger the guard:")
    hist = [["t1", 100.0, "HKD"], ["t2", 98.0, "HKD"], ["t3", 103.0, "HKD"],
            ["t4", 99.5, "HKD"]]
    cleaned, removed = heal_series(hist)
    check("nothing removed", removed, [])
    check("cleaned == original", cleaned, hist)


def test_heal_series_empty_and_singleton():
    print("heal_series: edge cases (empty / single point):")
    check("empty in -> empty out", heal_series([]), ([], []))
    one = [["t1", 100.0, "HKD"]]
    check("single point untouched", heal_series(one), (one, []))


def test_is_nl_implausible():
    print("is_nl_implausible:")
    check("no baseline yet -> always accepted", is_nl_implausible(0.0, None), False)
    check("baseline<=0 -> always accepted", is_nl_implausible(50.0, 0.0), False)
    check("drop to zero vs positive baseline -> implausible", is_nl_implausible(0.0, 10_040.0), True)
    check("negative reading -> implausible", is_nl_implausible(-500.0, 10_040.0), True)
    check("within 0.5x-2x band -> plausible", is_nl_implausible(15_000.0, 10_040.0), False)
    check("just above 2x -> implausible", is_nl_implausible(20_100.0, 10_040.0), True)
    check("just below 0.5x -> implausible", is_nl_implausible(5_000.0, 10_040.0), True)
    check("exactly at 2x boundary -> plausible", is_nl_implausible(20_080.0, 10_040.0), False)
    check("exactly at 0.5x boundary -> plausible", is_nl_implausible(5_020.0, 10_040.0), False)
    check("unchanged -> plausible", is_nl_implausible(10_040.0, 10_040.0), False)


def test_pending_confirms():
    print("pending_confirms:")
    check("no pending yet -> never confirms", pending_confirms(None, 0.0), False)
    check("pending==0.0 CAN confirm (not falsy)", pending_confirms(0.0, 0.0), True)
    check("matching value within tol -> confirms", pending_confirms(100.0, 100.005), True)
    check("outside tol -> does not confirm", pending_confirms(100.0, 101.0), False)
    check("different anomaly value -> does not confirm", pending_confirms(0.0, 50.0), False)


def test_is_equity_jump_implausible():
    print("is_equity_jump_implausible:")
    check("no baseline yet -> always plausible", is_equity_jump_implausible(10_000.0, 0.0, 0.0), False)
    check("drop to zero -> implausible", is_equity_jump_implausible(0.0, 10_040.0, 0.0), True)
    # FLAT (no open positions): tight noise-band check, regardless of jump size in ratio terms
    check("flat, tiny noise -> plausible", is_equity_jump_implausible(10_090.0, 10_040.0, 0.0), False)
    check("flat, exactly at noise-band boundary -> plausible",
          is_equity_jump_implausible(10_140.0, 10_040.0, 0.0), False)  # noise_band = max(100, 50.2) = 100
    check("flat, just past noise-band boundary -> implausible",
          is_equity_jump_implausible(10_140.01, 10_040.0, 0.0), True)
    # THE KEY REGRESSION CHECK: a ~30% deposit-sized jump used to be MISSED (within the old
    # 0.5x-2.0x band) -- now correctly flagged while flat, since nothing legitimate explains it.
    check("flat, ~30% deposit-sized jump -> now correctly implausible (was missed before)",
          is_equity_jump_implausible(13_000.0, 10_040.0, 0.0), True)
    # a large confirmed jump (the actual live incident) still correctly flagged too
    check("flat, ~10x jump -> implausible", is_equity_jump_implausible(99_994.0, 10_040.0, 0.0), True)
    # WITH open positions: falls back to the wider ratio band (mark-to-market P&L is legitimate)
    check("open positions, 30% move -> plausible (within wide band)",
          is_equity_jump_implausible(13_000.0, 10_040.0, 5_000.0), False)
    check("open positions, >2x move -> implausible",
          is_equity_jump_implausible(21_000.0, 10_040.0, 5_000.0), True)
    # gpv unknown (None, e.g. a connection hiccup before GrossPositionValue populates) -> must
    # NOT be treated as "flat" (we don't actually know) -- falls back to the wide band
    check("gpv unknown -> falls back to wide band, 30% move plausible",
          is_equity_jump_implausible(13_000.0, 10_040.0, None), False)


# ADDED 2026-07-21: broker reconciliation (STATE["reconcile"], the System Health banner's
# "reconcile:" line) used to run ONLY on a fresh IB connection -- once a real mismatch (CWB's
# ghost entry) was found, STATE["reconcile"] never got refreshed again on a stable, never-
# reconnecting connection, so the banner showed "mismatch found" indefinitely, surviving any
# number of browser refreshes, even though the underlying issue was long since fixed.
def test_reconcile_due():
    print("reconcile_due():")
    now = dt.datetime(2026, 7, 21, 12, 0, 0)
    check("never run before (None) -> due immediately", reconcile_due(None, now), True)
    check("just ran (0s ago) -> not due yet",
          reconcile_due(now, now, periodic_sec=600), False)
    check("ran 599s ago -> not due yet (just under the period)",
          reconcile_due(now - dt.timedelta(seconds=599), now, periodic_sec=600), False)
    check("ran exactly 600s ago -> due (boundary)",
          reconcile_due(now - dt.timedelta(seconds=600), now, periodic_sec=600), True)
    check("ran 20min ago -> due", reconcile_due(now - dt.timedelta(minutes=20), now,
                                                periodic_sec=600), True)
    check("default periodic_sec matches RECONCILE_PERIODIC_SEC (600s)",
          reconcile_due(now - dt.timedelta(seconds=601), now), True)


# ADDED 2026-07-27: a real HKD 30,000 monthly deposit landed on the LIVE account while 9 ETF
# positions were open and was counted as trading profit (P&L displayed 32,071 HKD vs a true
# 2,066 -- a 15x overstatement). is_equity_jump_implausible() only tightens its band while
# FLAT (no open positions) -- with positions open it falls back to a wide 0.5x-2.0x ratio band,
# and 132102/102120=1.29 sails straight through since magnitude alone can't distinguish a 29%
# deposit from a 29% market move. detect_external_cash_flow() replaces magnitude with the
# structural cash-vs-position-value signature (NetLiq = cash + GPV, verified exactly against
# the live account: 102,095.55 + 29,968.62 = 132,064.17), which works regardless of position
# state. See service.py's case table for the full reasoning.
def test_hist_cash_gpv():
    print("hist_cash_gpv():")
    check("legacy 3-field entry (pre-2026-07-27) -> (None, None), not (0, 0)",
          hist_cash_gpv([1785129096, 132101.90, "HKD"]), (None, None))
    check("new 5-field entry -> (cash, gpv)",
          hist_cash_gpv([1785129096, 132101.90, "HKD", 29968.62, 102079.09]),
          (29968.62, 102079.09))


def test_detect_external_cash_flow():
    print("\ndetect_external_cash_flow():")
    # THE REAL 2026-07-27 INCIDENT, reproduced exactly from the live snapshot
    # (prev_nl/prev_cash and new_nl/new_cash derive from the same cash+positions numbers).
    got = detect_external_cash_flow(102047.62, -31.38, 132047.62, 29968.62, 132047.62)
    approx("real deposit case: cash -31.38 -> 29968.62, positions untouched -> +30000",
           got, 30000.0, tol=0.01)
    check("withdrawal: cash drops with equity by the same amount -> negative flow",
          detect_external_cash_flow(132000.0, 30000.0, 112000.0, 10000.0, 112000.0), -20000.0)
    check("buy fill: cash down, equity unchanged -> None (money moved into positions)",
          detect_external_cash_flow(132000.0, 30000.0, 132000.0, 10000.0, 132000.0), None)
    check("sell fill: cash up, equity unchanged -> None",
          detect_external_cash_flow(132000.0, 10000.0, 132000.0, 30000.0, 132000.0), None)
    check("market move only: cash untouched -> None (this IS trading P&L)",
          detect_external_cash_flow(131968.0, 29968.0, 134008.0, 29968.0, 134008.0), None)
    check("small dividend-sized cash bump -> None (below the noise floor, stays in P&L)",
          detect_external_cash_flow(131968.0, 29968.0, 132468.0, 30468.0, 132468.0), None)
    check("legacy entry (prev cash unknown) -> None (caller falls back to magnitude check)",
          detect_external_cash_flow(None, None, 132047.86, 29968.62, 132047.86), None)
    # ---- THE 2026-09-29 INCIDENT (the old d_cash+d_gpv version booked these as flows) ----
    # Actual equity_history rows from the HYD/CWB short unwind: cash swung by >1.4M HKD per
    # window while NetLiq barely moved (short-covering is equity-neutral; the old version
    # used IBKR's ABSOLUTE GrossPositionValue, so abs-GPV shrank WITH cash and the residual
    # looked like a withdrawal -- 3 phantom rows, -4.87M HKD total).
    check("short-cover window (the 19:41 row): cash -1.42M, equity +5.4k -> None",
          detect_external_cash_flow(1119381.48, 706882.94, 1124741.66, -714965.33,
                                    1124741.66), None)
    check("short-cover window (the 19:44 row): cash -975k, equity -173 -> None",
          detect_external_cash_flow(1124741.66, -714965.33, 1124568.37, -1689538.78,
                                    1124568.37), None)
    check("flatten window (the 19:46 row): cash +1.59M, equity -151 -> None",
          detect_external_cash_flow(1124568.37, -1689538.78, 1124417.75, -103568.66,
                                    1124417.75), None)
    check("short CREATION (the 2026-09-01 oversell class): cash +X, equity ~flat -> None",
          detect_external_cash_flow(1100000.0, 200000.0, 1100000.0, 1600000.0,
                                    1100000.0), None)
    # Documents WHY layer 1 exists: the OLD magnitude-only heuristic really did miss this.
    check("regression check: the pre-existing magnitude heuristic missed this exact deposit",
          is_equity_jump_implausible(132101.90, 102119.95, 90000.0), False)


def _isolated_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    return old, path


def _restore_db(old, path):
    if old is None:
        os.environ.pop("DASH_DB_NAME", None)
    else:
        os.environ["DASH_DB_NAME"] = old
    try:
        os.remove(path)
    except OSError:
        pass


def _insert_closed_trade(c, realized_r=3.0, risk_money=1000.0):
    c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, entry, sl, "
             "tp, rr, size_units, status, exit_ts, exit_price, realized_r) VALUES "
             "(1,'2026-07-01T00:00:00','SPY','long','ATR rr3.0',400,390,430,3.0,10,'WIN',"
             f"'2026-07-10T00:00:00',430,{realized_r})")
    c.execute("INSERT INTO ib_mirror VALUES "
             f"(1,0,111,'SPY',10.0,{risk_money},'','2026-07-01T00:00:00','CLOSED','')")


def test_pnl_crosscheck_agrees_when_clean():
    print("\npnl_crosscheck(): equity route and trade route agree on an ordinary, "
          "correctly-recorded history:")
    old, path = _isolated_db()
    old_broker = os.environ.get("BROKER")
    os.environ["BROKER"] = "ib"
    try:
        from dashboard.core import paper, store
        from dashboard.execution import ib_exec   # local import: creates the ib_mirror table
        from dashboard.web import service
        with paper._LOCK, paper._conn():
            pass                                    # ensures paper_trades exists first
        with paper._LOCK, ib_exec._conn() as c:
            _insert_closed_trade(c, realized_r=3.0, risk_money=1000.0)   # +3000 USD realized
        store.cache_set("equity_history", [[1000, 100000.0, "USD"], [2000, 103000.0, "USD"]])
        store.cache_set("cash_flows", [])
        service.STATE["account"] = {"NetLiquidation": 103000.0, "_ccy": "USD"}
        service.STATE["positions"] = {}
        result = service.pnl_crosscheck()
        check("ok is True", result["ok"], True)
        approx("equity_pl", result["equity_pl"], 3000.0)
        approx("trade_pl", result["trade_pl"], 3000.0)
        approx("gap ~0", result["gap"], 0.0, tol=0.01)
    finally:
        if old_broker is None: os.environ.pop("BROKER", None)
        else: os.environ["BROKER"] = old_broker
        _restore_db(old, path)


def test_pnl_crosscheck_flags_unrecorded_deposit():
    print("\npnl_crosscheck(): reproduces the REAL 2026-07-27 incident -- an unrecorded "
          "30,000 deposit makes the equity route diverge sharply from the trade route:")
    old, path = _isolated_db()
    old_broker = os.environ.get("BROKER")
    os.environ["BROKER"] = "ib"
    try:
        from dashboard.core import paper, store
        from dashboard.execution import ib_exec
        from dashboard.web import service
        with paper._LOCK, paper._conn():
            pass
        with paper._LOCK, ib_exec._conn() as c:
            _insert_closed_trade(c, realized_r=3.0, risk_money=1000.0)   # trade route: +3000
        # equity jumped +33000 total, but the deposit was NEVER logged to cash_flows -- exactly
        # what happened live (the deposit landed, is_equity_jump_implausible() didn't flag it
        # because positions were open, so nothing ever wrote it to cash_flows)
        store.cache_set("equity_history", [[1000, 100000.0, "USD"], [2000, 133000.0, "USD"]])
        store.cache_set("cash_flows", [])
        service.STATE["account"] = {"NetLiquidation": 133000.0, "_ccy": "USD"}
        service.STATE["positions"] = {}
        result = service.pnl_crosscheck()
        check("ok is False -- the divergence is caught", result["ok"], False)
        approx("equity_pl (inflated by the missed deposit)", result["equity_pl"], 33000.0)
        approx("trade_pl (unaffected -- correctly excludes the deposit)", result["trade_pl"], 3000.0)
        approx("gap equals exactly the missed deposit", result["gap"], 30000.0)
    finally:
        if old_broker is None: os.environ.pop("BROKER", None)
        else: os.environ["BROKER"] = old_broker
        _restore_db(old, path)


def test_pnl_crosscheck_not_enough_data():
    print("\npnl_crosscheck(): no funded trades yet -> ok is None (can't judge), not a "
          "false positive on a brand-new account:")
    old, path = _isolated_db()
    try:
        from dashboard.core import paper, store
        from dashboard.web import service
        with paper._LOCK, paper._conn():
            pass
        store.cache_set("equity_history", [[1000, 100000.0, "USD"], [2000, 100000.0, "USD"]])
        store.cache_set("cash_flows", [])
        service.STATE["account"] = {"NetLiquidation": 100000.0, "_ccy": "USD"}
        service.STATE["positions"] = {}
        result = service.pnl_crosscheck()
        check("ok is None", result["ok"], None)
    finally:
        _restore_db(old, path)


# ADDED 2026-07-30, alongside the same-day fix for OPEN positions' stale price
# (ib_exec.py::live_positions()'s current_price). PENDING (not-yet-funded) trades have no
# broker position to read a fresh mark from, so they were still showing STATE["live"]'s WEEKLY-
# bar price under BROKER=ib -- confirmed live, same root cause, just for unfunded signals.
def test_refresh_pending_ticks_fetches_only_for_pending_instruments():
    print("_refresh_pending_ticks(): fetches a fresh IB tick for a PENDING (unfunded) "
          "instrument, but does NOT waste a call on an already-funded one:")
    old, path = _isolated_db()
    old_broker = os.environ.get("BROKER")
    os.environ["BROKER"] = "ib"
    try:
        from unittest import mock
        from dashboard.core import paper
        from dashboard.execution import ib_exec   # local import: creates the ib_mirror table
        from dashboard.web import service

        with paper._LOCK, paper._conn():
            pass
        with paper._LOCK, ib_exec._conn() as c:
            # id=1 SPY: OPEN, funded (has an ib_mirror row) -- must NOT get a tick fetch
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-07-21T00:00:00','SPY','long','ATR rr3.0',742.09,727.87,"
                     "784.76,3.0,1,'OPEN')")
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(1,0,111,'SPY',1.0,1000.0,'','2026-07-21T00:00:00','OPEN','etf')")
            # id=2 QQQ: OPEN, NOT funded (no ib_mirror row) -- pending, SHOULD get a fresh tick
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(2,'2026-07-21T00:00:00','QQQ','long','ATR rr3.0',675.49,633.32,"
                     "686.65,3.0,1,'OPEN')")
            # id=3 EIMI: OPEN, NOT funded -- UCITS/LSEETF key: its ib_exchange must be
            # threaded through as primaryExchange (2026-09-29: without it every fetch
            # threw IB error 200 -> red notable event -> Telegram push every cycle)
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(3,'2026-09-29T00:00:00','EIMI','long','ATR rr3.0',55.04,53.86,"
                     "58.57,3.0,1,'OPEN')")

        calls = []

        def _fake_tick(symbol, currency="USD", primary_exchange=""):
            calls.append((symbol, primary_exchange))
            return {"bid": 664.0, "ask": 664.74, "mid": 664.37, "spread": 0.74}

        service.STATE["live"] = {"QQQ": {"price": 675.49, "src": "yfinance",
                                         "spread": None, "age": None}}
        with mock.patch("dashboard.data.ib_client.get_stock_tick", side_effect=_fake_tick):
            service._refresh_pending_ticks()

        check("fetched a tick for each unfunded instrument",
              sorted(s for s, _ in calls), ["EIMI", "QQQ"])
        check("SPY (funded) got no fetch", "SPY" in [s for s, _ in calls], False)
        px = dict(calls)
        check("UCITS key passes its ib_exchange as primaryExchange",
              px.get("EIMI"), "LSEETF")
        check("US ticker passes no primaryExchange", px.get("QQQ"), "")
        check("QQQ's stale weekly price replaced with the fresh tick",
              service.STATE["live"]["QQQ"]["price"], 664.37)
        check("marked with the ib-tick source", service.STATE["live"]["QQQ"]["src"], "ib-tick")
    finally:
        if old_broker is None: os.environ.pop("BROKER", None)
        else: os.environ["BROKER"] = old_broker
        _restore_db(old, path)
        service.STATE["live"] = {}


def test_refresh_pending_ticks_noop_under_mt5():
    print("\n_refresh_pending_ticks(): MT5's STATE[\"live\"] is already tick-fresh -- "
          "no-op, zero IB calls, regardless of pending trades:")
    old, path = _isolated_db()
    old_broker = os.environ.get("BROKER")
    os.environ.pop("BROKER", None)      # default is mt5, not ib
    try:
        from unittest import mock
        from dashboard.core import paper
        from dashboard.web import service

        with paper._LOCK, paper._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-07-21T00:00:00','QQQ','long','ATR rr3.0',675.49,633.32,"
                     "686.65,3.0,1,'OPEN')")

        with mock.patch("dashboard.data.ib_client.get_stock_tick") as fake_tick:
            service._refresh_pending_ticks()
        check("get_stock_tick never called under MT5", fake_tick.called, False)
    finally:
        if old_broker is None: os.environ.pop("BROKER", None)
        else: os.environ["BROKER"] = old_broker
        _restore_db(old, path)


def test_refresh_pending_ticks_noop_when_nothing_pending():
    print("\n_refresh_pending_ticks(): no OPEN trades at all -- no-op, zero IB calls:")
    old, path = _isolated_db()
    old_broker = os.environ.get("BROKER")
    os.environ["BROKER"] = "ib"
    try:
        from unittest import mock
        from dashboard.core import paper
        from dashboard.web import service

        with paper._LOCK, paper._conn():
            pass   # paper_trades exists but is empty

        with mock.patch("dashboard.data.ib_client.get_stock_tick") as fake_tick:
            service._refresh_pending_ticks()
        check("get_stock_tick never called with nothing pending", fake_tick.called, False)
    finally:
        if old_broker is None: os.environ.pop("BROKER", None)
        else: os.environ["BROKER"] = old_broker
        _restore_db(old, path)


def test_open_position_instruments_includes_retired_but_open_keys():
    print("\n_open_position_instruments(): ADDED 2026-08-19 -- an OPEN position on a key "
          "retired from the active universe (e.g. IEF, retired under the UCITS swap) is "
          "still included so its chart/facts keep refreshing, but a CLOSED trade on a "
          "retired key is NOT (nothing left to keep live), and an already-ACTIVE key "
          "isn't duplicated:")
    old_broker = os.environ.get("BROKER")
    old_uni = os.environ.get("UNIVERSE")
    os.environ["BROKER"] = "ib"
    os.environ["UNIVERSE"] = "etf"
    old, path = _isolated_db()
    try:
        from dashboard.core import paper
        from dashboard.web import service
        with paper._LOCK, paper._conn() as c:
            # IEF: retired key, OPEN -- should be picked up
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-08-18T00:00:00','IEF','long','ATR rr3.0',100,95,115,3.0,10,'OPEN')")
            # TLT: retired key, but CLOSED -- should NOT be picked up
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(2,'2026-08-01T00:00:00','TLT','long','ATR rr3.0',80,75,95,3.0,10,'WIN')")
            # CSPX: currently-active key, OPEN -- already covered by active_universe() itself
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(3,'2026-08-18T00:00:00','CSPX','long','ATR rr3.0',800,750,950,3.0,1,'OPEN')")

        out = {i.key for i in service._open_position_instruments()}
        check("retired-but-OPEN key (IEF) included", "IEF" in out, True)
        check("retired-and-CLOSED key (TLT) excluded", "TLT" in out, False)
        check("already-active key (CSPX) not duplicated here", "CSPX" in out, False)
    finally:
        _restore_db(old, path)
        if old_broker is None: os.environ.pop("BROKER", None)
        else: os.environ["BROKER"] = old_broker
        if old_uni is None: os.environ.pop("UNIVERSE", None)
        else: os.environ["UNIVERSE"] = old_uni


# ---- compute_today_pnl() regressions (2026-09-30) ------------------------------------
# THE 2026-09-29 INCIDENT: on the short-unwind day this panel showed strategy -2.45M (the
# removed gpv-estimate fallback, fooled by ABS GPV + shorts), SGOV -1.69M (a share SALE
# priced as a loss), FX +2.4M, total +740,803 -- while equity actually rose +3,713. Three
# independent bugs, one per formula; each has its own assertion below.
def _today_pnl_env(hist, cash_flows, sgov_hist, account, positions, sweep):
    """Install a controlled STATE/cache world for one compute_today_pnl() call.
    Returns (service, store, saved_keys) for cleanup in finally."""
    from dashboard.core import store
    from dashboard.web import service
    saved = {k: service.STATE.get(k) for k in
             ("account", "positions", "cash_sweep", "fx_usd_per_base", "tbill_rate")}
    service.STATE["account"] = account
    service.STATE["positions"] = positions
    service.STATE["cash_sweep"] = sweep
    service.STATE["fx_usd_per_base"] = None
    service.STATE["tbill_rate"] = None
    store.cache_set("equity_history", hist)
    store.cache_set("cash_flows", cash_flows)
    store.cache_set("sgov_history", sgov_hist)
    store.cache_set("interest_history", [])
    store.cache_set("position_day_open", None)
    store.cache_set("daily_pnl_history", [])
    return service, store, saved


def _today_pnl_restore(saved):
    from dashboard.core import store
    from dashboard.web import service
    for k, v in saved.items():
        if v is None:
            service.STATE.pop(k, None)
        else:
            service.STATE[k] = v
    for k in ("equity_history", "cash_flows", "sgov_history", "interest_history",
              "position_day_open", "daily_pnl_history"):
        store.cache_set(k, [] if k != "position_day_open" else None)


def test_chart_pnl_view_excludes_the_inception_anchor():
    print("\nCHART FIX 2026-10-04: the P&L(ex-deposits) chart must not be anchored on -- or "
          "have its y-axis set by -- the hand-set [ts, 0.00] inception row.")
    from dashboard.core import store, paper

    anchor_ts = 1783468844                     # 2026-07-08, the live inception backfill
    # anchor + 32 days of real readings; the FIRST real reading is deliberately NOT 0 so a
    # wrong zero-reference is detectable.
    hist = [[anchor_ts, 0.0, "HKD"],
            [anchor_ts + 32 * 86400, 222111.11, "HKD"],
            [anchor_ts + 33 * 86400, 223000.00, "HKD"],
            [anchor_ts + 40 * 86400, 225000.00, "HKD"]]
    flows = [[anchor_ts + 10, 249971.50, "HKD"]]
    store.cache_set("chart_fix_hist", hist)

    with mock.patch.object(store, "cache_get",
                           side_effect=lambda k: ((hist, "t") if k == "equity_history"
                                                  else ((flows, "t") if k == "cash_flows"
                                                        else (None, None)))), \
         mock.patch.object(store, "cache_set", lambda *a, **k: None), \
         mock.patch.object(paper, "with_inception", wraps=paper.with_inception):
        full = paper.with_inception(hist)
        adj = paper.deposit_adjusted_series(full, flows)
        check("with_inception still prepends the anchor (card/drawdown depend on it)",
              full[0][1], 0.0)
        check("anchor's adj value is 0 (deposit netted out)", round(adj[0], 2), 0.0)

        # what the CHART does post-fix: drop the anchor, anchor P&L on the first REAL reading
        anchor_ts_used = full[0][0] if full[0][1] == 0.0 else None
        plot_idx = [i for i, h in enumerate(full) if h[0] != anchor_ts_used]
        pl_anchor_adj = adj[plot_idx[0]]
        ys = [adj[i] - pl_anchor_adj for i in plot_idx]

        check("anchor excluded from the plotted window", anchor_ts_used in plot_idx, False)
        check("plotted P&L starts at exactly 0", round(ys[0], 2), 0.0)
        check("plotted P&L ends positive (real gain preserved)",
              round(ys[-1], 2) > 0, True)
        # the regression: subtracting hist[0][1] (the anchor's 0.00) instead of the first
        # REAL reading is what crushed every value into a sliver under a fabricated origin.
        wrong = [adj[i] - full[0][1] for i in plot_idx]
        check("old zero-reference produced a large fake offset",
              abs(wrong[0]) > 1000, True)
        check("new zero-reference has no such offset", abs(ys[0]) < 0.01, True)

    store.cache_set("chart_fix_hist", [])


def test_today_pnl_total_excludes_only_recorded_flows():
    print("\ncompute_today_pnl() -- total: subtracts only RECORDED external flows, never "
          "internal cash<->positions moves (the +740,803 incident):")
    old, path = _isolated_db()
    old_broker = os.environ.get("BROKER")
    os.environ["BROKER"] = "ib"
    try:
        import time as _t
        from dashboard.core import paper
        from dashboard.execution import ib_exec   # creates ib_mirror
        with paper._LOCK, paper._conn():
            pass
        now = _t.time()
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        ts_start = now - 3600
        hist = [[ts_start, 1119381.48, "HKD", 706882.94, 5298025.29]]
        # equity now 1,123,094.33; cash crashed to -30,207 (the SGOV sale, an INTERNAL
        # move the old formula wrongly subtracted: it showed 3712.85 - (-737090) = +740,803)
        account = {"NetLiquidation": 1123094.33, "_ccy": "HKD",
                   "TotalCashValue": -30207.0, "GrossPositionValue": 1161231.0,
                   "AccruedCash": 0.0}
        # flat strategy book -- old gpv-estimate branch would have seen abs-GPV 5.3M ->
        # 1.16M and reported a fake -2.45M "strategy loss"
        service, store, saved = _today_pnl_env(hist, [], [], account, {}, {"enabled": False})
        try:
            out = service.compute_today_pnl()
            approx("total == real equity change (+3,712.85), not +740,803",
                   out["total"]["pnl"], 3712.85, tol=0.01)
            check("flat book => strategy 0.0 (gpv-estimate branch removed)",
                  out["strategy"]["total"], 0.0)
            check("strategy source is broker", out["strategy"]["source"], "broker")
            # now WITH a genuine recorded deposit of +10,000 today
            store.cache_set("cash_flows", [[now - 600, 10000.0, "HKD"]])
            out2 = service.compute_today_pnl()
            approx("recorded +10,000 deposit IS excluded: 3,712.85 - 10,000",
                   out2["total"]["pnl"], -6287.15, tol=0.01)
        finally:
            _today_pnl_restore(saved)
    finally:
        if old_broker is None: os.environ.pop("BROKER", None)
        else: os.environ["BROKER"] = old_broker
        _restore_db(old, path)


def test_today_pnl_sgov_share_aware():
    print("\ncompute_today_pnl() -- SGOV: a share SALE is not a loss; only price/yield "
          "movement on held shares counts (the -1.69M 'SGOV yield' incident):")
    old, path = _isolated_db()
    old_broker = os.environ.get("BROKER")
    os.environ["BROKER"] = "ib"
    try:
        import time as _t
        from dashboard.core import paper
        from dashboard.execution import ib_exec
        with paper._LOCK, paper._conn():
            pass
        now = _t.time()
        ts_start = now - 3600
        # start of day: 3,620 sh @ 785.187 HKD = 2,842,378.04 (row WITH qty, post-fix)
        hist = [[ts_start, 1119381.48, "HKD", 706882.94, 5298025.29]]
        sgov_hist = [[ts_start, 2842378.04, 3620.0]]
        # now: 1,470 sh after selling 2,150 to fund the unwind; px walked up to 785.287
        # (+0.10 HKD/sh = the day's yield) => true pnl = 3620 * 0.10 ~= +362
        sweep = {"enabled": True, "sgov_value_base": 1154371.90, "sgov_qty": 1470.0}
        account = {"NetLiquidation": 1123094.33, "_ccy": "HKD", "AccruedCash": 0.0}
        service, store, saved = _today_pnl_env(hist, [], sgov_hist, account, {}, sweep)
        try:
            out = service.compute_today_pnl()
            approx("share-aware pnl ~= +361 (yield on held shares), NOT -1.69M",
                   out["sgov"]["pnl"], 360.91, tol=1.0)
            # legacy row (no qty field) -> falls back to the raw value delta (documents
            # WHY the repair backfills qty: without it a sale day reads as a loss)
            store.cache_set("sgov_history", [[ts_start, 2842378.04]])
            out2 = service.compute_today_pnl()
            approx("legacy no-qty row falls back to value delta",
                   out2["sgov"]["pnl"], 1154371.90 - 2842378.04, tol=0.01)
            # shares unchanged (normal day): plain value delta IS the yield -- exercise the
            # sh_start == sh_now branch with a qty-bearing row on both sides
            store.cache_set("sgov_history", [[ts_start, 2842378.04, 3620.0]])
            sweep["sgov_qty"] = 3620.0
            sweep["sgov_value_base"] = 2842740.00
            service.STATE["cash_sweep"] = sweep
            out3 = service.compute_today_pnl()
            approx("unchanged shares: value delta == yield",
                   out3["sgov"]["pnl"], 2842740.00 - 2842378.04, tol=0.01)
        finally:
            _today_pnl_restore(saved)
    finally:
        if old_broker is None: os.environ.pop("BROKER", None)
        else: os.environ["BROKER"] = old_broker
        _restore_db(old, path)


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
