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


if __name__ == "__main__":
    test_cap_qty_to_portfolio_room()
    test_mirror_new_dd_halt_end_to_end()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
