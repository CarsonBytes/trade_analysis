"""Unit tests for the PURE sanity-guard functions in web/service.py -- equity-history
self-heal and the account-summary confirm-then-accept guard. Run:
  uv run python -m dashboard.tests.test_service
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile

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
    got = detect_external_cash_flow(-31.38, 102079.0, 29968.62, 102079.0, 132047.86)
    approx("real deposit case: cash -31.38 -> 29968.62, gpv unchanged -> +30000", got, 30000.0, tol=0.01)
    check("withdrawal: cash drops, gpv unchanged -> negative flow",
          detect_external_cash_flow(30000.0, 102000.0, 10000.0, 102000.0, 112000.0), -20000.0)
    check("buy fill: cash down, gpv up by the same amount -> None (not an external flow)",
          detect_external_cash_flow(30000.0, 102000.0, 10000.0, 122000.0, 132000.0), None)
    check("sell fill: cash up, gpv down by the same amount -> None",
          detect_external_cash_flow(10000.0, 122000.0, 30000.0, 102000.0, 132000.0), None)
    check("market move only: cash untouched -> None (this IS trading P&L)",
          detect_external_cash_flow(29968.0, 102000.0, 29968.0, 104040.0, 134008.0), None)
    check("small dividend-sized cash bump -> None (below the noise floor, stays in P&L)",
          detect_external_cash_flow(29968.0, 102000.0, 30468.0, 102000.0, 132468.0), None)
    check("legacy entry (cash/gpv unknown) -> None (caller must fall back to the magnitude check)",
          detect_external_cash_flow(None, None, 29968.62, 102079.0, 132047.86), None)
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

        calls = []

        def _fake_tick(symbol):
            calls.append(symbol)
            return {"bid": 664.0, "ask": 664.74, "mid": 664.37, "spread": 0.74}

        service.STATE["live"] = {"QQQ": {"price": 675.49, "src": "yfinance",
                                         "spread": None, "age": None}}
        with mock.patch("dashboard.data.ib_client.get_stock_tick", side_effect=_fake_tick):
            service._refresh_pending_ticks()

        check("fetched exactly one tick", calls, ["QQQ"])
        check("SPY (funded) got no fetch", "SPY" in calls, False)
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
