"""Unit tests for the PURE sizing logic in execution/ib_exec.py -- the portfolio-level
gross-exposure cap -- PLUS an end-to-end integration check of the DD_HALT_PCT gate in
mirror_new() itself (not just the pure current_drawdown_pct() function it calls), since
that gate has never fired for real (grepped the live log: zero "DD-halt:" lines ever) and
a bug in the pure function alone already came close to bricking live trading once this
session (2026-07-11, see HANDOFF -- the -90% "drawdown" bug). Testing the pure function in
isolation isn't enough to trust the WIRING (mirror_new() actually reading the right cache
keys, actually short-circuiting before any order-placement code runs, actually returning the
halt message) -- this exercises mirror_new() itself with the IB connection mocked out.
Run:  uv run python -m dashboard.tests.test_ib_exec
"""
from __future__ import annotations

import os
import tempfile
import datetime as dt
from unittest import mock

from dashboard.execution.ib_exec import (cap_qty_to_portfolio_room, commission_estimate_usd,
                                         is_commission_viable)

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def test_cap_qty_to_portfolio_room():
    print("cap_qty_to_portfolio_room:")
    # equity=$100k, portfolio_cap=1.0 (100%), price=$100/share throughout
    check("plenty of room -> qty unchanged",
          cap_qty_to_portfolio_room(50, 100.0, 100_000.0, 1.0, 0.0), 50)
    check("partial room -> qty scaled down to fit exactly",
          cap_qty_to_portfolio_room(150, 100.0, 100_000.0, 1.0, 90_000.0), 100)
    check("exactly at boundary -> qty unaffected (fits exactly)",
          cap_qty_to_portfolio_room(100, 100.0, 100_000.0, 1.0, 90_000.0), 100)
    check("already deployed AT the cap -> qty forced to 0",
          cap_qty_to_portfolio_room(50, 100.0, 100_000.0, 1.0, 100_000.0), 0)
    check("already deployed OVER the cap -> qty forced to 0, not negative",
          cap_qty_to_portfolio_room(50, 100.0, 100_000.0, 1.0, 110_000.0), 0)
    check("portfolio_cap=0 -> disabled, qty unchanged regardless of deployed",
          cap_qty_to_portfolio_room(500, 100.0, 100_000.0, 0.0, 999_000.0), 500)
    check("negative portfolio_cap -> also disabled (defensive)",
          cap_qty_to_portfolio_room(500, 100.0, 100_000.0, -1.0, 0.0), 500)
    check("price<=0 -> disabled (guard), qty unchanged",
          cap_qty_to_portfolio_room(500, 0.0, 100_000.0, 1.0, 0.0), 500)
    check("tighter portfolio_cap (0.5) with no prior deployment",
          cap_qty_to_portfolio_room(1000, 100.0, 100_000.0, 0.5, 0.0), 500)
    check("qty=0 in -> qty=0 out (never scales UP)",
          cap_qty_to_portfolio_room(0, 100.0, 100_000.0, 1.0, 0.0), 0)


def test_mirror_new_dd_halt_end_to_end():
    print("mirror_new() DD_HALT_PCT gate (end-to-end, IB connection mocked):")
    from dashboard.execution import ib_exec

    # a real, deep drawdown: peak 100 -> now 80 = -20%, well past the -13% default threshold
    halted_hist = [[100, 100.0, "HKD"], [200, 100.0, "HKD"], [300, 80.0, "HKD"]]
    # a shallow drawdown: peak 100 -> now 95 = -5%, should NOT halt
    ok_hist = [[100, 100.0, "HKD"], [200, 100.0, "HKD"], [300, 95.0, "HKD"]]

    def _cache_get(hist):
        def fn(key):
            if key == "equity_history":
                return hist, "2026-07-11T00:00:00"
            if key == "cash_flows":
                return None, None
            raise AssertionError(f"unexpected cache_get({key!r}) -- test should not reach here")
        return fn

    # pin DD_HALT_PCT explicitly (don't inherit whatever the test-running shell has set)
    with mock.patch.dict(os.environ, {"DD_HALT_PCT": "-13.0"}), \
         mock.patch.object(ib_exec, "_guard", return_value=object()), \
         mock.patch.object(ib_exec.store, "cache_get", side_effect=_cache_get(halted_hist)):
        logs = ib_exec.mirror_new()
    check("deep drawdown (-20%) -> mirror_new() halts, returns exactly 1 log line",
          len(logs), 1)
    check("halt message names the real computed drawdown, not a placeholder",
          ("-20.0%" in logs[0]) if logs else False, True)
    check("halt message says 'DD-halt'", ("DD-halt" in logs[0]) if logs else False, True)

    # shallow drawdown must NOT halt -- stop it deterministically right after the DD check
    # (raise from _equity_usd, the next thing mirror_new() calls) instead of letting a real
    # `ib` sentinel fall through into an actual network connection attempt.
    class _StoppedHere(Exception):
        pass

    try:
        with mock.patch.dict(os.environ, {"DD_HALT_PCT": "-13.0"}), \
             mock.patch.object(ib_exec, "_guard", return_value=object()), \
             mock.patch.object(ib_exec.store, "cache_get", side_effect=_cache_get(ok_hist)), \
             mock.patch.object(ib_exec, "_equity_usd", side_effect=_StoppedHere):
            logs2 = ib_exec.mirror_new()
        took_halt_path = len(logs2) == 1 and "DD-halt" in logs2[0]
    except _StoppedHere:
        took_halt_path = False        # reached past the DD check = proof it did NOT halt
    check("shallow drawdown (-5%) -> does NOT take the DD-halt short-circuit",
          took_halt_path, False)


def test_pending_entry_notional_usd():
    print("_pending_entry_notional_usd(): FIXED 2026-07-13 -- GrossPositionValue alone "
          "misses pending (not-yet-filled) order commitment, confirmed live: 6 pending "
          "orders already totalled ~125% of equity before this fix existed:")
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, paper._conn() as _pc:   # ensures paper_trades table exists first
            pass
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-07-13T04:00:00','CPER','long','ATR rr3.0',38.0,36.7,42.0,3.0,84,'OPEN')")
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(2,'2026-07-13T04:00:00','EEM','long','ATR rr3.0',67.0,63.2,78.0,3.0,34,'OPEN')")
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(1,0,111,'CPER',84.0,50.0,'','2026-07-13T04:00:00','OPEN','etf')")
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(2,0,222,'EEM',34.0,50.0,'','2026-07-13T04:00:00','OPEN','etf')")

        with mock.patch.object(ib_exec.ib_client, "broker_open_order_symbols",
                              return_value={"CPER", "EEM"}), \
             mock.patch.object(ib_exec.ib_client, "broker_positions", return_value={}):
            total = ib_exec._pending_entry_notional_usd()
        check("sums qty x entry across both pending symbols",
              total, 84.0 * 38.0 + 34.0 * 67.0)

        # EEM already FILLED (a real broker position exists) -- must NOT double-count it
        # alongside GrossPositionValue, only CPER's pending notional should remain
        with mock.patch.object(ib_exec.ib_client, "broker_open_order_symbols",
                              return_value={"CPER", "EEM"}), \
             mock.patch.object(ib_exec.ib_client, "broker_positions",
                              return_value={"EEM": 34.0}):
            total2 = ib_exec._pending_entry_notional_usd()
        check("a filled symbol is excluded (no double-count with GrossPositionValue)",
              total2, 84.0 * 38.0)

        with mock.patch.object(ib_exec.ib_client, "broker_open_order_symbols",
                              return_value=None):
            total3 = ib_exec._pending_entry_notional_usd()
        check("broker unavailable (None) -> 0.0, fails safe", total3, 0.0)

        with mock.patch.object(ib_exec.ib_client, "broker_open_order_symbols",
                              return_value=set()):
            total4 = ib_exec._pending_entry_notional_usd()
        check("no pending orders -> 0.0", total4, 0.0)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_current_portfolio_room_usd():
    print("current_portfolio_room_usd(): PUBLIC accessor app.py's _pending_reason() uses "
          "to tell 'blocked by PORTFOLIO_CAP' apart from 'awaiting the next mirror cycle' "
          "(2026-07-13 fix -- confirmed live: SPY/QQQ/IWM were mislabeled as the latter):")
    from dashboard.execution import ib_exec

    with mock.patch.object(ib_exec, "_guard", return_value=None):
        check("not connected -> None", ib_exec.current_portfolio_room_usd(), None)

    with mock.patch.dict(os.environ, {"PORTFOLIO_CAP": "0"}), \
         mock.patch.object(ib_exec, "_guard", return_value=object()):
        check("PORTFOLIO_CAP disabled (0) -> None (no meaningful room to report)",
              ib_exec.current_portfolio_room_usd(), None)

    with mock.patch.dict(os.environ, {"PORTFOLIO_CAP": "1.0"}), \
         mock.patch.object(ib_exec, "_guard", return_value=object()), \
         mock.patch.object(ib_exec, "_equity_usd", return_value=100_000.0), \
         mock.patch.object(ib_exec, "_gpv_usd", return_value=80_000.0), \
         mock.patch.object(ib_exec, "_pending_entry_notional_usd", return_value=15_000.0):
        check("equity 100k, cap 100%, 80k filled + 15k pending -> 5k room left",
              ib_exec.current_portfolio_room_usd(), 5_000.0)

    with mock.patch.dict(os.environ, {"PORTFOLIO_CAP": "1.0"}), \
         mock.patch.object(ib_exec, "_guard", return_value=object()), \
         mock.patch.object(ib_exec, "_equity_usd", return_value=100_000.0), \
         mock.patch.object(ib_exec, "_gpv_usd", return_value=90_000.0), \
         mock.patch.object(ib_exec, "_pending_entry_notional_usd", return_value=25_000.0):
        check("already OVER the cap -> room floors at 0.0, not negative",
              ib_exec.current_portfolio_room_usd(), 0.0)


def test_sync_closures_cancels_stale_order_when_paper_already_resolved():
    print("\nsync_closures(): paper resolved independently (EXPIRED) while a real order is "
          "still working at the broker -- must cancel it, not leave it orphaned forever "
          "(2026-07-13 fix: paper.resolve_open() runs regardless of broker fill status, so "
          "a trade can resolve via real price/horizon while its bracket order never filled):")
    from types import SimpleNamespace
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, paper._conn() as _pc:   # ensures paper_trades table exists first
            pass
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-06-01T00:00:00','CPER','long','ATR rr3.0',38.0,36.7,42.0,3.0,"
                     "84,'EXPIRED')")
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(1,0,111,'CPER',84.0,50.0,'','2026-06-01T00:00:00','OPEN','etf')")

        cancelled = []
        fake_order = SimpleNamespace(orderId=1)
        fake_trade = SimpleNamespace(
            contract=SimpleNamespace(conId=111),
            orderStatus=SimpleNamespace(status="Submitted"),
            order=fake_order,
        )

        class _FakeIB:
            def cancelOrder(self, order):
                cancelled.append(order)
            def positions(self):
                return []

        fake_ib = _FakeIB()
        with mock.patch.object(ib_exec, "_guard", return_value=fake_ib), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec.ib_client, "_run", return_value=[fake_trade]), \
             mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, **kw: fn()):
            logs = ib_exec.sync_closures()

        check("cancelled exactly one order", len(cancelled), 1)
        check("cancelled the correct order object", cancelled[0] is fake_order, True)
        check("logged the cancellation, naming the resolved status",
              any("cancelled stale unfilled order" in l and "EXPIRED" in l for l in logs), True)
        with ib_exec._conn() as c:
            status = c.execute("SELECT status FROM ib_mirror WHERE paper_id=1").fetchone()[0]
        check("ib_mirror row marked CLOSED to match", status, "CLOSED")
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_sync_closures_closes_stale_mirror_row_when_position_still_open_via_other_layer():
    print("\nsync_closures(): TWO paper trades share one broker position (layered ATR "
          "signals funding the same aggregate con_id) -- the OLDER one resolves independently "
          "(e.g. via the deterministic tick path) while the broker's AGGREGATE position stays "
          "non-zero because the NEWER layer still holds it. Before the 2026-08-05 fix, the "
          "older trade's ib_mirror row never got marked CLOSED (fell through every existing "
          "branch, which only closed rows once the aggregate position went FLAT), causing "
          "live_positions() to attach the same real position to BOTH paper_ids -- confirmed "
          "live: double-counted the pie chart's market value and, once, exploded a trade's "
          "displayed R-multiple to +24.3R by pairing the broker's cross-layer blended avgCost "
          "with one specific trade's own unrelated stop-loss:")
    from types import SimpleNamespace
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, paper._conn() as _pc:   # ensures paper_trades table exists first
            pass
        with paper._LOCK, ib_exec._conn() as c:
            # older trade: already resolved (LOSS), mirror row still (wrongly) OPEN
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-06-01T00:00:00','CPER','long','ATR rr3.0',37.45,36.2,40.0,3.0,"
                     "40,'LOSS')")
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(1,0,111,'CPER',40.0,50.0,'','2026-06-01T00:00:00','OPEN','etf')")
            # newer trade: genuinely OPEN, same con_id (layered into the same broker position)
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(2,'2026-07-20T00:00:00','CPER','long','ATR rr3.0',39.28,38.17,44.0,3.0,"
                     "44,'OPEN')")
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(2,0,111,'CPER',44.0,55.0,'','2026-07-20T00:00:00','OPEN','etf')")

        fake_pos = SimpleNamespace(contract=SimpleNamespace(conId=111), position=84.0)

        class _FakeIB:
            def positions(self):
                return [fake_pos]

        fake_ib = _FakeIB()
        with mock.patch.object(ib_exec, "_guard", return_value=fake_ib), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec.ib_client, "_run", return_value=[]), \
             mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, **kw: fn()):
            ib_exec.sync_closures()

        with ib_exec._conn() as c:
            s1 = c.execute("SELECT status FROM ib_mirror WHERE paper_id=1").fetchone()[0]
            s2 = c.execute("SELECT status FROM ib_mirror WHERE paper_id=2").fetchone()[0]
        check("older, already-resolved trade's mirror row is now CLOSED", s1, "CLOSED")
        check("newer, genuinely open trade's mirror row stays OPEN", s2, "OPEN")
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_sync_closures_does_not_orphan_the_last_mirror_row_for_a_position():
    print("\nsync_closures(): a SINGLE resolved paper trade, no other layer -- the aggregate "
          "broker position is still non-zero (resolving via the deterministic tick path does "
          "NOT itself sell the broker position) but this is the ONLY ib_mirror row for that "
          "con_id. The 2026-08-05 same-day fix above closed this unconditionally -- correct "
          "for CPER/CWB (a newer layer keeps tracking it) but WRONG here: closing the LAST "
          "open row for a con_id orphans a REAL, still-open position with ZERO local tracking "
          "anywhere (live_positions() only reads status='OPEN' rows) -- confirmed live: VNQ "
          "and QQQ both went fully broker-only-invisible this way within hours of that fix's "
          "first deploy, caught only because the project's OWN broker reconciliation "
          "separately flagged 'broker-only (no local record): [\"QQQ\", \"VNQ\"]'. Must leave "
          "the row OPEN when nothing else is tracking that con_id:")
    from types import SimpleNamespace
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, paper._conn() as _pc:
            pass
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-06-24T00:00:00','VNQ','long','ATR rr3.0',97.86,94.67,110.0,3.0,"
                     "15.7,'EXPIRED')")
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(1,0,222,'VNQ',15.7,50.0,'','2026-06-24T00:00:00','OPEN','etf')")

        fake_pos = SimpleNamespace(contract=SimpleNamespace(conId=222), position=203.0)

        class _FakeIB:
            def positions(self):
                return [fake_pos]

        fake_ib = _FakeIB()
        with mock.patch.object(ib_exec, "_guard", return_value=fake_ib), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec.ib_client, "_run", return_value=[]), \
             mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, **kw: fn()):
            ib_exec.sync_closures()

        with ib_exec._conn() as c:
            s1 = c.execute("SELECT status FROM ib_mirror WHERE paper_id=1").fetchone()[0]
        check("the ONLY mirror row for this position stays OPEN (not orphaned)", s1, "OPEN")
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


# ADDED 2026-07-21: sync_closures() previously had no terminal outcome for a bracket ENTRY
# that was rejected/cancelled at the broker before ever filling -- only for exits (a real
# position that later closed). Confirmed live: CWB's 2026-07-20 order vanished from the
# broker (no fill, no position, no working order) but stayed marked OPEN in both
# paper_trades and ib_mirror indefinitely -- only caught by reconcile_with_broker(), which
# only runs once per fresh IB connection, not every cycle.
def test_sync_closures_auto_cancels_dead_entry_past_grace_period():
    print("\nsync_closures(): a bracket entry that never filled at the broker (no position, "
          "no working order, no closing fill -- CWB's real 2026-07-20 case) gets "
          "auto-cancelled once GHOST_ENTRY_GRACE_MIN has passed, instead of staying OPEN "
          "forever:")
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, paper._conn() as _pc:
            pass
        old_ts = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(minutes=ib_exec.GHOST_ENTRY_GRACE_MIN + 15)).isoformat(timespec="seconds")
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,?,'CWB','long','ATR rr3.0',101.79,98.52,111.60,3.0,2,'OPEN')", (old_ts,))
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(1,0,333,'CWB',2.0,129.0,'',?,'OPEN','etf')", (old_ts,))

        class _FakeIB:
            def positions(self):
                return []
            def fills(self):
                return []

        with mock.patch.object(ib_exec, "_guard", return_value=_FakeIB()), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec.ib_client, "_run", return_value=[]), \
             mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, **kw: fn()):
            logs = ib_exec.sync_closures()

        check("logged the auto-cancellation", any("auto-cancelled" in l for l in logs), True)
        with ib_exec._conn() as c:
            pstatus = c.execute("SELECT status FROM paper_trades WHERE id=1").fetchone()[0]
            mstatus, note = c.execute(
                "SELECT status, note FROM ib_mirror WHERE paper_id=1").fetchone()
        check("paper_trades marked CANCELLED", pstatus, "CANCELLED")
        check("ib_mirror marked CLOSED", mstatus, "CLOSED")
        check("ib_mirror note explains why", "ghost" in note, True)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_sync_closures_leaves_recent_dead_entry_candidate_within_grace_period():
    print("\nsync_closures(): the SAME dead-entry pattern, but still within "
          "GHOST_ENTRY_GRACE_MIN -- left alone, re-checked next cycle (never wrongly "
          "cancels a real order still mid-flight):")
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, paper._conn() as _pc:
            pass
        recent_ts = (dt.datetime.now(dt.timezone.utc)
                    - dt.timedelta(minutes=2)).isoformat(timespec="seconds")
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,?,'CWB','long','ATR rr3.0',101.79,98.52,111.60,3.0,2,'OPEN')", (recent_ts,))
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(1,0,333,'CWB',2.0,129.0,'',?,'OPEN','etf')", (recent_ts,))

        class _FakeIB:
            def positions(self):
                return []
            def fills(self):
                return []

        with mock.patch.object(ib_exec, "_guard", return_value=_FakeIB()), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec.ib_client, "_run", return_value=[]), \
             mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, **kw: fn()):
            logs = ib_exec.sync_closures()

        check("NOT auto-cancelled yet (within grace period)",
              any("auto-cancelled" in l for l in logs), False)
        with ib_exec._conn() as c:
            pstatus = c.execute("SELECT status FROM paper_trades WHERE id=1").fetchone()[0]
            mstatus = c.execute("SELECT status FROM ib_mirror WHERE paper_id=1").fetchone()[0]
        check("paper_trades still OPEN", pstatus, "OPEN")
        check("ib_mirror still OPEN", mstatus, "OPEN")
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_resolve_from_broker_elevates_loss_to_warning_level():
    print("\n_resolve_from_broker(): a real (broker-funded) LOSS is recorded at 'warning' "
          "level so it actually pushes to Telegram -- a WIN stays 'info' (2026-07-18: "
          "notify.py only pushes warning/error, so an unflagged real loss would silently "
          "never buzz the phone -- exactly the event the reentry-gate work needs surfaced):")
    from types import SimpleNamespace
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, paper._conn() as _pc:   # ensures paper_trades table exists first
            pass
        loss_trade = {"id": 1, "instrument": "CWB", "direction": "long",
                     "entry": 103.85, "sl": 100.0, "half_spread": 0.0}
        win_trade = {"id": 2, "instrument": "SPY", "direction": "long",
                    "entry": 500.0, "sl": 490.0, "half_spread": 0.0}

        class _FakeIB:
            def fills(self):
                return [
                    SimpleNamespace(contract=SimpleNamespace(conId=111),
                                   execution=SimpleNamespace(avgPrice=95.0, price=95.0)),
                    SimpleNamespace(contract=SimpleNamespace(conId=222),
                                   execution=SimpleNamespace(avgPrice=510.0, price=510.0)),
                ]

        recorded: list[tuple[str, str]] = []
        with mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, **kw: fn()), \
             mock.patch("dashboard.core.notable_events.record",
                        side_effect=lambda msg, level="info": recorded.append((msg, level))):
            loss_msg = ib_exec._resolve_from_broker(_FakeIB(), loss_trade, 111)
            win_msg = ib_exec._resolve_from_broker(_FakeIB(), win_trade, 222)

        check("resolved as a real LOSS", "LOSS" in loss_msg, True)
        check("resolved as a real WIN", "WIN" in win_msg, True)
        check("LOSS pushed at warning level", recorded[0][1], "warning")
        check("WIN stays at info level (no extra phone buzz)", recorded[1][1], "info")
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


# ADDED 2026-07-18: a queued (not-yet-funded) ETF signal can sit behind PORTFOLIO_CAP for
# days -- but _place_etf_bracket() funds at a FRESH market price while keeping the STALE
# stop/target from signal time, so a signal that's drifted far enough against itself before
# ever being funded would enter with a badly distorted, unintended risk profile (confirmed
# live: SPY/DIA had drifted ~63% of the way to their own stale stops while still unfunded).
# Backtested first (delayed-funding simulation, research/backtest.py): cancelling at
# drifted R <= STALE_SIGNAL_CANCEL_R (-0.5) improved aggregate meanR +26-30% across every
# queue-delay window tested, correctly identifying a net-negative cohort.
def test_stale_signal_check_cancels_when_drifted_past_threshold():
    print("_stale_signal_check(): a long signal that's drifted past -0.5R against its own "
          "entry/stop (using a live price near the stale stop) gets flagged for cancellation:")
    from dashboard.execution import ib_exec
    # entry=100, sl=95 -> risk_dist=5. -0.5R = 97.5. Price at 96 is well past that (-0.8R).
    # XAUUSD used as the test instrument -- it's in BY_KEY unconditionally (a default-universe
    # key), unlike ETF keys (SPY etc.) which only populate BY_KEY when UNIVERSE=etf is set at
    # dashboard.instruments' IMPORT time -- too late to fix from inside a test function.
    t = {"instrument": "XAUUSD", "direction": "long", "entry": 100.0, "sl": 95.0}
    with mock.patch("dashboard.data.providers.get_live_price", return_value=(96.0, "test", None)):
        reason, price, drifted_r = ib_exec._stale_signal_check(t)
    check("cancel reason is set", reason is not None, True)
    check("mentions the actual drifted R", ("-0.80R" in reason) if reason else False, True)
    check("returns the live price used", price, 96.0)
    check("returns the computed drifted R", round(drifted_r, 2), -0.80)


def test_stale_signal_check_leaves_fresh_signals_alone():
    print("\n_stale_signal_check(): a signal still within threshold (or moving favorably) "
          "is left alone -- no cancellation:")
    from dashboard.execution import ib_exec
    t = {"instrument": "XAUUSD", "direction": "long", "entry": 100.0, "sl": 95.0}
    # only -0.2R drifted -- comfortably inside the -0.5R threshold
    with mock.patch("dashboard.data.providers.get_live_price", return_value=(99.0, "test", None)):
        reason, price, drifted_r = ib_exec._stale_signal_check(t)
    check("no cancellation for mild drift", reason, None)
    check("still returns price/drifted_r for the caller", (price, round(drifted_r, 2)), (99.0, -0.20))
    # a SHORT signal that's moved favorably (price fell) must not cancel either
    t_short = {"instrument": "XAUUSD", "direction": "short", "entry": 100.0, "sl": 105.0}
    with mock.patch("dashboard.data.providers.get_live_price", return_value=(98.0, "test", None)):
        reason_s, _, drifted_r_s = ib_exec._stale_signal_check(t_short)
    check("short signal moving favorably -> no cancellation", reason_s, None)
    check("favorable drift is positive R", drifted_r_s > 0, True)


def test_stale_signal_check_fails_open_on_no_live_price():
    print("\n_stale_signal_check(): no live price available (data gap) -> fails OPEN, never "
          "blocks a real entry over missing data:")
    from dashboard.execution import ib_exec
    t = {"instrument": "XAUUSD", "direction": "long", "entry": 100.0, "sl": 95.0}
    with mock.patch("dashboard.data.providers.get_live_price", return_value=(None, "none", None)):
        reason, price, drifted_r = ib_exec._stale_signal_check(t)
    check("no price -> no cancellation (fail open)", reason, None)
    check("no price -> price is None", price, None)


def test_mirror_new_cancels_stale_signal_instead_of_funding():
    print("\nmirror_new(): a stale unfunded ETF signal gets CANCELLED in the paper journal "
          "and _place_etf_bracket() is NEVER called for it (end-to-end, IB mocked):")
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, paper._conn() as _pc:
            pass
        with paper._LOCK, ib_exec._conn() as c:
            # NOTE 2026-07-30: IEF (not SPY) -- SPY joined TECH_TICKERS that day, and this
            # test is exercising the unrelated stale-signal check, not the tech-pause gate,
            # which would otherwise cancel it first for a different reason.
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-07-13T04:00:00','IEF','long','ATR rr3.0',100.0,95.0,115.0,3.0,10,'OPEN')")

        place_calls = []

        def _fake_place_etf_bracket(ib, t, equity, acct=None, deployed=None):
            place_calls.append(t["id"])
            return f"{t['instrument']}: should NOT have been called"

        # BY_KEY only contains ETF entries when UNIVERSE=etf was set at dashboard.instruments'
        # IMPORT time (too late to fix here) -- ETF_TRADED_BY_KEY is unconditionally populated
        # so the ETF branch is reached fine, but _stale_signal_check()'s BY_KEY.get("IEF")
        # lookup needs a stand-in; get_live_price is mocked below anyway so it doesn't care
        # what instrument object it's called with.
        with mock.patch.dict(os.environ, {"DD_HALT_PCT": "0"}), \
             mock.patch.dict("dashboard.instruments.BY_KEY", {"IEF": object()}), \
             mock.patch.object(ib_exec, "_guard", return_value=object()), \
             mock.patch.object(ib_exec, "_mirrored_ids", return_value=set()), \
             mock.patch.object(ib_exec, "_equity_usd", return_value=100_000.0), \
             mock.patch.object(ib_exec, "_gpv_usd", return_value=0.0), \
             mock.patch.object(ib_exec, "_pending_entry_notional_usd", return_value=0.0), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec, "within_entry_execution_window", return_value=True), \
             mock.patch.object(ib_exec, "_place_etf_bracket", side_effect=_fake_place_etf_bracket), \
             mock.patch("dashboard.data.providers.get_live_price",
                        return_value=(90.0, "test", None)):    # -2.0R drift, well past threshold
            logs = ib_exec.mirror_new()

        check("_place_etf_bracket was never called for the stale signal",
              len(place_calls), 0)
        check("mirror_new() logged the cancellation",
              any("stale signal auto-cancelled" in l for l in logs), True)
        with ib_exec._conn() as c:
            status, exit_reason = c.execute(
                "SELECT status, exit_reason FROM paper_trades WHERE id=1").fetchone()
        check("paper_trades row marked CANCELLED", status, "CANCELLED")
        check("exit_reason explains why", "stale signal auto-cancelled" in exit_reason, True)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


# ADDED 2026-07-30: cancel_pending_order(), called from the dashboard's per-trade "Withdraw"
# button BEFORE paper.withdraw_trade() touches the local journal -- must never leave a real
# resting order at the broker with nothing local tracking it anymore (same failure mode
# sync_closures() already guards against elsewhere in this file, above).
def test_cancel_pending_order_cancels_the_resting_order():
    print("cancel_pending_order(): a resting broker order for this paper trade gets "
          "cancelled, and the ib_mirror row is marked CLOSED:")
    from types import SimpleNamespace
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, paper._conn() as _pc:
            pass
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(7,0,222,'SPY',1.0,50.0,'','2026-07-30T00:00:00','OPEN','etf')")

        cancelled = []
        fake_order = SimpleNamespace(orderId=1)
        fake_trade = SimpleNamespace(contract=SimpleNamespace(conId=222), order=fake_order)

        class _FakeIB:
            def openTrades(self):
                return [fake_trade]
            def cancelOrder(self, order):
                cancelled.append(order)

        with mock.patch.object(ib_exec, "_guard", return_value=_FakeIB()), \
             mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, **kw: fn()):
            msg = ib_exec.cancel_pending_order(7)

        check("cancelled exactly one order", len(cancelled), 1)
        check("cancelled the correct order object", cancelled[0] is fake_order, True)
        check("returned a human message naming the trade id", "7" in (msg or ""), True)
        with ib_exec._conn() as c:
            status = c.execute("SELECT status FROM ib_mirror WHERE paper_id=7").fetchone()[0]
        check("ib_mirror row marked CLOSED", status, "CLOSED")
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_cancel_pending_order_noop_when_never_reached_broker():
    print("\ncancel_pending_order(): no ib_mirror row at all (most pending trades -- still "
          "waiting on risk budget/account size, never sent to IB) -- safe no-op:")
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, paper._conn() as _pc:
            pass

        class _FakeIB:
            def openTrades(self):
                raise AssertionError("should never be called -- nothing to look up")
            def cancelOrder(self, order):
                raise AssertionError("should never be called")

        with mock.patch.object(ib_exec, "_guard", return_value=_FakeIB()):
            msg = ib_exec.cancel_pending_order(999)
        check("returns None", msg, None)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_cancel_pending_order_noop_when_broker_unreachable():
    print("\ncancel_pending_order(): broker not connected (_guard() -> None) -> safe no-op, "
          "no exception:")
    from dashboard.execution import ib_exec
    with mock.patch.object(ib_exec, "_guard", return_value=None):
        check("returns None", ib_exec.cancel_pending_order(1), None)


# ADDED 2026-07-30: manual tech pause (paper.TECH_PAUSED/TECH_TICKERS), user-requested. Covers
# the THIRD of the three places this gate applies (evaluate_signal() and place_sleeve_signals()
# are tested in test_evaluate_signal.py / test_sleeve.py) -- an already-pending, not-yet-funded
# CORE tech signal must be actively CANCELLED here, not just left queued.
# CHANGED 2026-07-30 (same day): uses method='ATR rr3.0' (core), not 'dipbuy-sleeve' -- the
# sleeve is now deliberately EXEMPT from this gate (see test_mirror_new_lets_pending_sleeve_
# tech_signal_through_when_paused() below), so a sleeve-method row here would no longer get
# cancelled and this test would be testing the wrong thing.
def test_mirror_new_cancels_pending_tech_signal_when_paused():
    print("\nmirror_new(): TECH_PAUSED=True actively cancels an already-pending CORE QQQ "
          "signal -- _place_etf_bracket() is never called for it:")
    from dashboard.execution import ib_exec
    from dashboard.core import paper

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old_db = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    old_tech_paused = paper.TECH_PAUSED
    paper.TECH_PAUSED = True
    try:
        with paper._LOCK, paper._conn() as _pc:
            pass
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-07-29T23:48:00','QQQ','long','ATR rr3.0',666.65,633.32,"
                     "686.65,3.0,1,'OPEN')")

        etf_calls = []

        with mock.patch.dict(os.environ, {"DD_HALT_PCT": "0"}), \
             mock.patch.object(ib_exec, "_guard", return_value=object()), \
             mock.patch.object(ib_exec, "_mirrored_ids", return_value=set()), \
             mock.patch.object(ib_exec, "_equity_usd", return_value=100_000.0), \
             mock.patch.object(ib_exec, "_gpv_usd", return_value=0.0), \
             mock.patch.object(ib_exec, "_pending_entry_notional_usd", return_value=0.0), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec, "_place_etf_bracket",
                              side_effect=lambda *a, **kw: etf_calls.append(1)):
            logs = ib_exec.mirror_new()

        check("_place_etf_bracket never called", len(etf_calls), 0)
        check("mirror_new() logged the cancellation",
              any("tech investment paused" in l for l in logs), True)
        with ib_exec._conn() as c:
            status, exit_reason, realized_r = c.execute(
                "SELECT status, exit_reason, realized_r FROM paper_trades WHERE id=1").fetchone()
        check("paper_trades row marked CANCELLED", status, "CANCELLED")
        check("exit_reason explains why", exit_reason, "tech investment paused")
        check("realized_r is flat (0) -- never funded, no real gain/loss", realized_r, 0.0)
    finally:
        paper.TECH_PAUSED = old_tech_paused
        if old_db is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old_db
        try:
            os.remove(path)
        except OSError:
            pass


# ADDED 2026-07-30: the priority-order fix itself -- a pending SLEEVE (dipbuy-sleeve) tech
# signal must NOT be cancelled even while TECH_PAUSED=True, and must reach normal placement.
# User request: tech-pause is now lower priority than the dipbuy-sleeve strategy.
# ADDED 2026-07-31: execution-window gate (user-requested) -- holds new-entry order
# SUBMISSION to 10:00am-3:30pm ET, avoiding the wider open/close spreads. Entries only,
# skips (doesn't cancel) outside the window.
def test_within_entry_execution_window_boundaries():
    print("within_entry_execution_window(): boundary checks (10:00/15:30 ET edges, "
          "weekend, DST):")
    from dashboard.execution import ib_exec
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    cases = [
        ("Mon 09:59:59 ET (1s before open)", dt.datetime(2026, 8, 3, 9, 59, 59, tzinfo=ET), False),
        ("Mon 10:00:00 ET (exactly open)", dt.datetime(2026, 8, 3, 10, 0, 0, tzinfo=ET), True),
        ("Mon 12:00 ET (midday)", dt.datetime(2026, 8, 3, 12, 0, tzinfo=ET), True),
        ("Mon 15:30:00 ET (exactly cutoff)", dt.datetime(2026, 8, 3, 15, 30, 0, tzinfo=ET), True),
        ("Mon 15:30:01 ET (1s after cutoff)", dt.datetime(2026, 8, 3, 15, 30, 1, tzinfo=ET), False),
        ("Sat 12:00 ET (weekend)", dt.datetime(2026, 8, 1, 12, 0, tzinfo=ET), False),
        ("Dec Mon 10:00 ET (EST/winter, exactly open)", dt.datetime(2026, 12, 7, 10, 0, tzinfo=ET), True),
    ]
    for label, t, want in cases:
        check(label, ib_exec.within_entry_execution_window(t), want)


def test_mirror_new_holds_entry_outside_execution_window():
    print("\nmirror_new(): outside 10:00am-3:30pm ET, a pending CORE entry is held (not "
          "placed, not cancelled) -- _place_etf_bracket() never called:")
    from dashboard.execution import ib_exec
    from dashboard.core import paper

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old_db = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    old_tech_paused = paper.TECH_PAUSED
    paper.TECH_PAUSED = False   # isolate this gate from the unrelated tech-pause gate
    try:
        with paper._LOCK, paper._conn() as _pc:
            pass
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-07-29T23:48:00','DIA','long','ATR rr3.0',400.0,380.0,"
                     "460.0,3.0,1,'OPEN')")

        etf_calls = []
        with mock.patch.dict(os.environ, {"DD_HALT_PCT": "0"}), \
             mock.patch.object(ib_exec, "_guard", return_value=object()), \
             mock.patch.object(ib_exec, "_mirrored_ids", return_value=set()), \
             mock.patch.object(ib_exec, "_equity_usd", return_value=100_000.0), \
             mock.patch.object(ib_exec, "_gpv_usd", return_value=0.0), \
             mock.patch.object(ib_exec, "_pending_entry_notional_usd", return_value=0.0), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec, "within_entry_execution_window", return_value=False), \
             mock.patch.object(ib_exec, "_place_etf_bracket",
                              side_effect=lambda *a, **kw: etf_calls.append(1)):
            ib_exec.mirror_new()

        check("_place_etf_bracket never called (outside window)", len(etf_calls), 0)
        with ib_exec._conn() as c:
            status = c.execute("SELECT status FROM paper_trades WHERE id=1").fetchone()[0]
        check("paper_trades row still OPEN (held, not cancelled)", status, "OPEN")
    finally:
        paper.TECH_PAUSED = old_tech_paused
        if old_db is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old_db
        try:
            os.remove(path)
        except OSError:
            pass


def test_mirror_new_places_entry_inside_execution_window():
    print("\nmirror_new(): inside 10:00am-3:30pm ET, a pending CORE entry reaches "
          "_place_etf_bracket() normally:")
    from dashboard.execution import ib_exec
    from dashboard.core import paper

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old_db = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    old_tech_paused = paper.TECH_PAUSED
    paper.TECH_PAUSED = False
    try:
        with paper._LOCK, paper._conn() as _pc:
            pass
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-07-29T23:48:00','DIA','long','ATR rr3.0',400.0,380.0,"
                     "460.0,3.0,1,'OPEN')")

        etf_calls = []
        with mock.patch.dict(os.environ, {"DD_HALT_PCT": "0"}), \
             mock.patch.object(ib_exec, "_guard", return_value=object()), \
             mock.patch.object(ib_exec, "_mirrored_ids", return_value=set()), \
             mock.patch.object(ib_exec, "_equity_usd", return_value=100_000.0), \
             mock.patch.object(ib_exec, "_gpv_usd", return_value=0.0), \
             mock.patch.object(ib_exec, "_pending_entry_notional_usd", return_value=0.0), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec, "within_entry_execution_window", return_value=True), \
             mock.patch.object(ib_exec, "_place_etf_bracket",
                              side_effect=lambda *a, **kw: etf_calls.append(1)):
            ib_exec.mirror_new()

        check("_place_etf_bracket WAS called (inside window)", len(etf_calls), 1)
    finally:
        paper.TECH_PAUSED = old_tech_paused
        if old_db is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old_db
        try:
            os.remove(path)
        except OSError:
            pass


def test_mirror_new_lets_pending_sleeve_tech_signal_through_when_paused():
    print("\nmirror_new(): TECH_PAUSED=True does NOT touch a pending SLEEVE QQQ signal -- "
          "it reaches _place_sleeve_bracket() same as if tech-pause didn't exist:")
    from dashboard.execution import ib_exec
    from dashboard.core import paper

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old_db = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    old_tech_paused = paper.TECH_PAUSED
    paper.TECH_PAUSED = True
    try:
        with paper._LOCK, paper._conn() as _pc:
            pass
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-07-29T23:48:00','QQQ','long','dipbuy-sleeve',666.65,633.32,"
                     "686.65,3.0,1,'OPEN')")

        sleeve_calls = []

        with mock.patch.dict(os.environ, {"DD_HALT_PCT": "0"}), \
             mock.patch.object(ib_exec, "_guard", return_value=object()), \
             mock.patch.object(ib_exec, "_mirrored_ids", return_value=set()), \
             mock.patch.object(ib_exec, "_equity_usd", return_value=100_000.0), \
             mock.patch.object(ib_exec, "_gpv_usd", return_value=0.0), \
             mock.patch.object(ib_exec, "_pending_entry_notional_usd", return_value=0.0), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec, "within_entry_execution_window", return_value=True), \
             mock.patch.object(ib_exec, "_place_sleeve_bracket",
                              side_effect=lambda *a, **kw: (sleeve_calls.append(1), "placed")[1]):
            logs = ib_exec.mirror_new()

        check("_place_sleeve_bracket WAS called (not blocked by tech-pause)",
              len(sleeve_calls), 1)
        check("mirror_new() did NOT log a tech-pause cancellation",
              any("tech investment paused" in l for l in logs), False)
        with ib_exec._conn() as c:
            status = c.execute("SELECT status FROM paper_trades WHERE id=1").fetchone()[0]
        check("paper_trades row still OPEN (not cancelled)", status, "OPEN")
    finally:
        paper.TECH_PAUSED = old_tech_paused
        if old_db is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old_db
        try:
            os.remove(path)
        except OSError:
            pass


def test_mirror_new_does_not_cancel_tech_signals_when_resumed():
    print("\nmirror_new(): TECH_PAUSED=False (resumed) -- a pending QQQ signal reaches "
          "normal placement, is NOT cancelled:")
    from dashboard.execution import ib_exec
    from dashboard.core import paper

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old_db = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    old_tech_paused = paper.TECH_PAUSED
    paper.TECH_PAUSED = False
    try:
        with paper._LOCK, paper._conn() as _pc:
            pass
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-07-29T23:48:00','QQQ','long','dipbuy-sleeve',666.65,633.32,"
                     "686.65,3.0,1,'OPEN')")

        sleeve_calls = []

        with mock.patch.dict(os.environ, {"DD_HALT_PCT": "0"}), \
             mock.patch.object(ib_exec, "_guard", return_value=object()), \
             mock.patch.object(ib_exec, "_mirrored_ids", return_value=set()), \
             mock.patch.object(ib_exec, "_equity_usd", return_value=100_000.0), \
             mock.patch.object(ib_exec, "_gpv_usd", return_value=0.0), \
             mock.patch.object(ib_exec, "_pending_entry_notional_usd", return_value=0.0), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec, "within_entry_execution_window", return_value=True), \
             mock.patch.object(ib_exec, "_place_sleeve_bracket",
                              side_effect=lambda *a, **kw: (sleeve_calls.append(1), "placed")[1]):
            ib_exec.mirror_new()

        check("_place_sleeve_bracket WAS called (not blocked)", len(sleeve_calls), 1)
        with ib_exec._conn() as c:
            status = c.execute("SELECT status FROM paper_trades WHERE id=1").fetchone()[0]
        check("paper_trades row still OPEN (not cancelled)", status, "OPEN")
    finally:
        paper.TECH_PAUSED = old_tech_paused
        if old_db is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old_db
        try:
            os.remove(path)
        except OSError:
            pass


# ADDED 2026-07-20: PORTFOLIO_CAP's "scale down, never skip" can compress a signal to 1-2
# shares when the account is near-fully deployed -- the position still funds, but its
# realized dollar risk shrinks with it while IBKR's per-order commission floor does NOT,
# so a severely-compressed fill can burn a large fraction of its own risk budget on fees
# alone. Real commission schedule confirmed via reqExecutions against the live account
# (Fixed plan: $0.005/share, $1.00/order min, capped at 1% of trade value -- NOT the
# Tiered schedule originally assumed, which understated real commission ~1.8-2.9x).
# Backtested first (real 22-ETF universe, PORTFOLIO_CAP-aware chronological walk): a 10%
# commission/risk floor is net-positive at today's live equity (+~1-3% cumulative over the
# last 3yrs) and roughly neutral once equity has compounded much larger. See HANDOFF.
def test_commission_estimate_usd():
    print("commission_estimate_usd(): matches this account's REAL confirmed IBKR fills "
          "(pulled via reqExecutions 2026-07-20):")
    # real fill: IWM 6sh @ $294.97 -> real commission $1.00 (the $1 floor -- per-share
    # calc 0.005*6=$0.03 is far below it, and the 1% cap ($17.70) doesn't bind here)
    check("IWM 6sh @ $294.97 -> $1.00 floor",
          round(commission_estimate_usd(6, 294.97), 4), 1.00)
    # real fill: EEM 1sh @ $63.98 -> real commission $0.6413 (the 1% cap, not the $1 floor,
    # binds below ~$100 notional -- 1%*63.98=$0.6398, matches the real fill to a cent)
    check("EEM 1sh @ $63.98 -> 1% cap binds, not the $1 floor",
          round(commission_estimate_usd(1, 63.98), 4), 0.6398)
    check("qty=0 -> $0", commission_estimate_usd(0, 100.0), 0.0)
    check("price<=0 -> $0 (guard)", commission_estimate_usd(10, 0.0), 0.0)


def test_is_commission_viable():
    print("\nis_commission_viable():")
    # EEM crumb case: qty=1 @ $63.98, realized_risk=$3.35 (stop_per_share ~3.35) -- round-trip
    # commission ~$1.28 is ~38% of that risk, well past the 10% default cap -> NOT viable.
    viable, pct = is_commission_viable(1, 63.98, 3.35)
    check("EEM-style crumb (qty=1, tiny risk) -> NOT viable", viable, False)
    check("commission_pct is ~38% (2*0.6398/3.35)", round(pct, 2), 0.38)
    # IWM normal case: qty=6 @ $294.97, realized_risk=$47.64 -- round-trip $2.00 is only ~4.2%
    # of that risk -> comfortably viable, matching how normal-sized trades pass through untouched.
    viable2, pct2 = is_commission_viable(6, 294.97, 47.64)
    check("IWM-style normal size -> viable", viable2, True)
    check("commission_pct is ~4.2%", round(pct2, 3), 0.042)
    # guard: realized_risk<=0 -> always viable (never divide by zero / never wrongly block)
    viable3, pct3 = is_commission_viable(5, 100.0, 0.0)
    check("zero risk -> viable (guard)", viable3, True)
    check("zero risk -> commission_pct 0.0", pct3, 0.0)
    # disabled (matches ETF_POS_CAP/PORTFOLIO_CAP's own 0-disables convention)
    from dashboard.execution import ib_exec
    with mock.patch.object(ib_exec, "MIN_VIABLE_COMMISSION_PCT", 0):
        viable4, pct4 = ib_exec.is_commission_viable(1, 63.98, 3.35)
    check("MIN_VIABLE_COMMISSION_PCT=0 -> disabled, always viable", viable4, True)


def test_place_etf_bracket_skips_commission_not_viable_crumb():
    print("\n_place_etf_bracket(): a PORTFOLIO_CAP-compressed crumb (today's real EEM "
          "scenario: $12.9k equity, ~$64 of room left, EEM~$64/share -> 1sh) is SKIPPED "
          "before any order is sent, and does NOT reserve portfolio-cap room:")
    from dashboard.execution import ib_exec

    t = {"id": 27, "instrument": "EEM", "direction": "long",
        "entry": 63.98, "sl": 60.63, "tp": 74.87}
    deployed = [12900.0 - 64.0]     # only ~$64 of room left before the 100% portfolio cap
    with mock.patch.object(ib_exec.paper, "RISK_PER_TRADE", 0.01), \
         mock.patch.dict(os.environ, {"ETF_POS_CAP": "0.30", "PORTFOLIO_CAP": "1.0"}), \
         mock.patch.object(ib_exec.ib_client, "stock_contract", return_value=object()), \
         mock.patch.object(ib_exec.ib_client, "call") as fake_call:
        msg = ib_exec._place_etf_bracket(ib=object(), t=t, equity_usd=12900.0, deployed=deployed)

    check("no order was sent", fake_call.called, False)
    check("message explains the commission-not-viable skip", "commission" in (msg or ""), True)
    check("deployed room was NOT reserved for a skipped order", deployed[0], 12900.0 - 64.0)


# ADDED 2026-07-30: live_positions() now reports the broker's own live mark as
# "current_price" -- confirmed live the dashboard had no fresh per-instrument price for a
# pure IB deployment (STATE["live"] falls back to WEEKLY yfinance bars under BROKER=ib,
# stale by up to a week -- QQQ showed 675.49, last Tuesday's close, vs a real 664.37) and
# silently fed a wrong unrealized-R figure too. See app.py::_trade_card()'s matching fix.
def test_live_positions_reports_current_price_from_broker():
    print("live_positions(): reports the broker's own live mark (portfolio marketPrice) as "
          "current_price, not just avgCost/unrealizedPNL:")
    from dashboard.execution import ib_exec

    class _Contract:
        def __init__(self, con_id):
            self.conId = con_id

    class _Position:
        def __init__(self, con_id, position, avg_cost):
            self.contract = _Contract(con_id)
            self.position = position
            self.avgCost = avg_cost

    class _PortfolioItem:
        def __init__(self, con_id, unrealized_pnl, market_price):
            self.contract = _Contract(con_id)
            self.unrealizedPNL = unrealized_pnl
            self.marketPrice = market_price

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(1,0,111,'QQQ',10.0,1000.0,'','2026-07-17T00:00:00','OPEN','etf')")

        class _FakeIB:
            def positions(self):
                return [_Position(111, 10, 692.42)]
            def portfolio(self):
                return [_PortfolioItem(111, -139.6, 664.37)]

        with mock.patch.object(ib_exec.ib_client, "is_available", return_value=True), \
             mock.patch.object(ib_exec.ib_client, "_ensure_conn", return_value=_FakeIB()), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec.ib_client, "filter_by_account",
                              side_effect=lambda items, acct: items), \
             mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, **kw: fn()):
            out = ib_exec.live_positions()
        check("position present", 1 in out, True)
        check("current_price comes from the broker's live mark, not avgCost",
              out[1]["current_price"], 664.37)
        check("open (entry) is still avgCost, unchanged", out[1]["open"], 692.42)
        check("profit still comes from unrealizedPNL, unchanged", out[1]["profit"], -139.6)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_live_positions_current_price_none_when_broker_omits_it():
    print("\nlive_positions(): missing/zero marketPrice -> current_price is None, not a "
          "silently wrong 0.0 (caller must fall back, not trust a fake price):")
    from dashboard.execution import ib_exec

    class _Contract:
        def __init__(self, con_id):
            self.conId = con_id

    class _Position:
        def __init__(self, con_id, position, avg_cost):
            self.contract = _Contract(con_id)
            self.position = position
            self.avgCost = avg_cost

    class _PortfolioItem:
        def __init__(self, con_id, unrealized_pnl, market_price):
            self.contract = _Contract(con_id)
            self.unrealizedPNL = unrealized_pnl
            self.marketPrice = market_price

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import paper
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(1,0,111,'QQQ',10.0,1000.0,'','2026-07-17T00:00:00','OPEN','etf')")

        class _FakeIB:
            def positions(self):
                return [_Position(111, 10, 692.42)]
            def portfolio(self):
                return [_PortfolioItem(111, 0.0, 0.0)]  # marketPrice 0.0 -- broker didn't report one

        with mock.patch.object(ib_exec.ib_client, "is_available", return_value=True), \
             mock.patch.object(ib_exec.ib_client, "_ensure_conn", return_value=_FakeIB()), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
             mock.patch.object(ib_exec.ib_client, "filter_by_account",
                              side_effect=lambda items, acct: items), \
             mock.patch.object(ib_exec.ib_client, "call", side_effect=lambda fn, **kw: fn()):
            out = ib_exec.live_positions()
        check("current_price is None, not the misleading 0.0", out[1]["current_price"], None)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_guard_paper_port_allowlist():
    print("\n_guard(): paper-mode port allowlist -- REGRESSION test for the 2026-08-13 bug "
          "where 4004 (the WSL2/Docker deployment's real paper-Gateway relay port) was "
          "missing from this tuple, silently refusing every trade attempt on that "
          "deployment since the 4002->4004 port fix went in. Confirms 7497/4002/4004 are "
          "all accepted and an arbitrary port is still rejected, so a future edit narrowing "
          "this list back down fails a test instead of failing silently in production:")
    from dashboard.execution import ib_exec

    sentinel = object()
    with mock.patch.object(ib_exec.ib_client, "is_available", return_value=True), \
         mock.patch.object(ib_exec, "is_paper", return_value=True), \
         mock.patch.object(ib_exec.ib_client, "_ensure_conn", return_value=sentinel):
        for port in (7497, 4002, 4004):
            with mock.patch.dict(os.environ, {"IB_PORT": str(port)}, clear=False):
                os.environ.pop("IB_ALLOW_LIVE", None)
                check(f"port {port} accepted", ib_exec._guard(), sentinel)
        with mock.patch.dict(os.environ, {"IB_PORT": "9999"}, clear=False):
            os.environ.pop("IB_ALLOW_LIVE", None)
            check("arbitrary port 9999 still rejected", ib_exec._guard(), None)


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
