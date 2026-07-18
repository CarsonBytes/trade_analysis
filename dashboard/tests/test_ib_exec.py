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
from unittest import mock

from dashboard.execution.ib_exec import cap_qty_to_portfolio_room

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


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


if __name__ == "__main__":
    test_cap_qty_to_portfolio_room()
    test_mirror_new_dd_halt_end_to_end()
    test_pending_entry_notional_usd()
    test_current_portfolio_room_usd()
    test_sync_closures_cancels_stale_order_when_paper_already_resolved()
    test_resolve_from_broker_elevates_loss_to_warning_level()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
