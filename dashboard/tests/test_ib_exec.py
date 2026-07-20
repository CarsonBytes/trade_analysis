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

from dashboard.execution.ib_exec import (cap_qty_to_portfolio_room, commission_estimate_usd,
                                         is_commission_viable)

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
            c.execute("INSERT INTO paper_trades (id, ts, instrument, direction, method, "
                     "entry, sl, tp, rr, size_units, status) VALUES "
                     "(1,'2026-07-13T04:00:00','SPY','long','ATR rr3.0',100.0,95.0,115.0,3.0,10,'OPEN')")

        place_calls = []

        def _fake_place_etf_bracket(ib, t, equity, acct=None, deployed=None):
            place_calls.append(t["id"])
            return f"{t['instrument']}: should NOT have been called"

        # BY_KEY only contains ETF entries when UNIVERSE=etf was set at dashboard.instruments'
        # IMPORT time (too late to fix here) -- ETF_TRADED_BY_KEY is unconditionally populated
        # so the ETF branch is reached fine, but _stale_signal_check()'s BY_KEY.get("SPY")
        # lookup needs a stand-in; get_live_price is mocked below anyway so it doesn't care
        # what instrument object it's called with.
        with mock.patch.dict(os.environ, {"DD_HALT_PCT": "0"}), \
             mock.patch.dict("dashboard.instruments.BY_KEY", {"SPY": object()}), \
             mock.patch.object(ib_exec, "_guard", return_value=object()), \
             mock.patch.object(ib_exec, "_mirrored_ids", return_value=set()), \
             mock.patch.object(ib_exec, "_equity_usd", return_value=100_000.0), \
             mock.patch.object(ib_exec, "_gpv_usd", return_value=0.0), \
             mock.patch.object(ib_exec, "_pending_entry_notional_usd", return_value=0.0), \
             mock.patch.object(ib_exec.ib_client, "account_id", return_value="U123"), \
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


if __name__ == "__main__":
    test_cap_qty_to_portfolio_room()
    test_mirror_new_dd_halt_end_to_end()
    test_pending_entry_notional_usd()
    test_current_portfolio_room_usd()
    test_sync_closures_cancels_stale_order_when_paper_already_resolved()
    test_resolve_from_broker_elevates_loss_to_warning_level()
    test_stale_signal_check_cancels_when_drifted_past_threshold()
    test_stale_signal_check_leaves_fresh_signals_alone()
    test_stale_signal_check_fails_open_on_no_live_price()
    test_mirror_new_cancels_stale_signal_instead_of_funding()
    test_commission_estimate_usd()
    test_is_commission_viable()
    test_place_etf_bracket_skips_commission_not_viable_crumb()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
