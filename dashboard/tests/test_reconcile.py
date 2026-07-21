"""Unit tests for broker reconciliation:
  - compare_positions() (core/reconcile.py) -- pure, no I/O.
  - mirrored_open_symbols() (execution/ib_exec.py) -- reads ib_mirror, isolated here
    against a throwaway temp sqlite db (never touches the real paper/live journal).
Run:  uv run python -m dashboard.tests.test_reconcile
"""
from __future__ import annotations

import os
import tempfile
from unittest import mock

from dashboard.core.reconcile import compare_positions

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


def test_compare_positions_clean_match():
    print("compare_positions: broker and local agree -> no mismatch:")
    out = compare_positions({"AAPL": 10.0, "MSFT": -5.0}, {"AAPL", "MSFT"})
    check("clean match", out, {"only_local": [], "only_broker": []})


def test_compare_positions_ghost_trade():
    print("compare_positions: local thinks OPEN, broker reports nothing (ghost):")
    out = compare_positions({}, {"AMLP", "ASHR"})
    check("ghost trades flagged", out, {"only_local": ["AMLP", "ASHR"], "only_broker": []})


def test_compare_positions_untracked_broker_position():
    print("compare_positions: broker holds a position with no local OPEN record:")
    out = compare_positions({"SPY": 3.0}, set())
    check("untracked position flagged", out, {"only_local": [], "only_broker": ["SPY"]})


def test_compare_positions_zero_qty_excluded():
    print("compare_positions: a broker row with qty==0 is NOT a real position:")
    out = compare_positions({"AAPL": 0.0, "MSFT": 5.0}, {"AAPL", "MSFT"})
    # AAPL has a local record but broker qty is 0 -> broker doesn't actually hold it -> ghost
    check("zero-qty broker row treated as flat", out, {"only_local": ["AAPL"], "only_broker": []})


def test_compare_positions_mixed():
    print("compare_positions: both directions mismatched at once:")
    out = compare_positions({"SPY": 3.0, "QQQ": 2.0}, {"QQQ", "EEM"})
    check("mixed mismatch", out, {"only_local": ["EEM"], "only_broker": ["SPY"]})


def test_compare_positions_empty_both():
    print("compare_positions: genuinely flat everywhere:")
    out = compare_positions({}, set())
    check("both empty -> no mismatch", out, {"only_local": [], "only_broker": []})


def test_compare_positions_pending_order_not_a_ghost():
    print("compare_positions: local OPEN, no broker POSITION, but a live pending ORDER "
          "-- must NOT be a ghost (2026-07-13 fix: 6 real GTC MKT orders placed before "
          "market open sat correctly unfilled for hours and were falsely flagged before this):")
    out = compare_positions({}, {"CPER", "EEM"}, broker_pending_symbols={"CPER", "EEM"})
    check("pending orders excluded from only_local", out, {"only_local": [], "only_broker": []})


def test_compare_positions_pending_order_mixed_with_real_ghost():
    print("compare_positions: one symbol has a pending order (fine), another has "
          "NEITHER a position NOR a pending order (a real ghost):")
    out = compare_positions({}, {"CPER", "AMLP"}, broker_pending_symbols={"CPER"})
    check("only the true ghost survives", out, {"only_local": ["AMLP"], "only_broker": []})


def test_compare_positions_no_pending_arg_unchanged():
    print("compare_positions: omitting broker_pending_symbols entirely -- old behavior intact:")
    out = compare_positions({}, {"AMLP", "ASHR"})
    check("still flags as ghosts (backward compatible)", out,
          {"only_local": ["AMLP", "ASHR"], "only_broker": []})


def test_compare_positions_excludes_cash_sweep_holding():
    print("\ncompare_positions: SGOV (the cash-sweep shield, intentionally never in "
          "ib_mirror) is excluded, not flagged as an untracked broker position -- "
          "confirmed live 2026-07-18: this fired a real 'position MISMATCH' alarm for "
          "exactly SGOV, which self-resolved 6 minutes later on its own -- not a real "
          "desync, a missing exclusion for an intentional non-strategy holding:")
    out = compare_positions({"SGOV": 500.0, "CPER": 30.0}, {"CPER"},
                            excluded_symbols={"SGOV"})
    check("SGOV excluded, real position unaffected", out, {"only_local": [], "only_broker": []})


def test_compare_positions_excluded_does_not_hide_a_real_ghost():
    print("\ncompare_positions: excluded_symbols only suppresses the excluded symbol -- "
          "a genuine untracked position elsewhere still gets flagged:")
    out = compare_positions({"SGOV": 500.0, "SPY": 3.0}, set(), excluded_symbols={"SGOV"})
    check("SPY still flagged, SGOV still excluded", out, {"only_local": [], "only_broker": ["SPY"]})


def test_compare_positions_no_excluded_arg_unchanged():
    print("\ncompare_positions: omitting excluded_symbols entirely -- old behavior intact "
          "(back-compat for any other caller):")
    out = compare_positions({"SGOV": 500.0}, set())
    check("SGOV flagged when no exclusion given", out, {"only_local": [], "only_broker": ["SGOV"]})


def test_mirrored_open_symbols_isolated_db():
    print("mirrored_open_symbols: reads ib_mirror from an isolated temp db:")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)                       # let sqlite create it fresh
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path     # paper._DB is lazy -- an absolute path overrides
    try:
        from dashboard.core import paper
        from dashboard.execution import ib_exec
        check("resolves to the temp path", str(paper._DB), path)
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO ib_mirror VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (1, 111, 222, "AMLP", 6.0, 50.0, "", "2026-07-08T00:00:00", "OPEN", "etf"))
            c.execute("INSERT INTO ib_mirror VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (2, 112, 223, "ASHR", 8.0, 60.0, "", "2026-07-09T00:00:00", "OPEN", "etf"))
            c.execute("INSERT INTO ib_mirror VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (3, 113, 224, "SPY", 1.0, 70.0, "", "2026-07-05T00:00:00", "CLOSED", "etf"))
        out = ib_exec.mirrored_open_symbols()
        check("only OPEN rows returned", out, {"AMLP", "ASHR"})
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


# ADDED 2026-07-21: reconcile_with_broker() only runs once per FRESH connection, and the
# "Recent notable events" panel just lists the last 20 changelog rows with no resolved/
# unresolved distinction -- a real mismatch (CWB's ghost entry) stayed visible there for
# hours after being fixed, with nothing to tell a viewer it was no longer active.
def test_reconcile_with_broker_records_cleared_after_previous_mismatch():
    print("\nreconcile_with_broker(): after a PREVIOUS mismatch, the next clean check "
          "records a 'mismatch CLEARED' follow-up (warning level, so it also reaches "
          "Telegram) -- so the history shows resolution, not just a dangling alarm:")
    from dashboard.core import reconcile, store
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        recorded = []
        with mock.patch.object(ib_exec, "mirrored_open_symbols", return_value=set()), \
             mock.patch("dashboard.data.ib_client.broker_positions", return_value={}), \
             mock.patch("dashboard.data.ib_client.broker_open_order_symbols", return_value=set()), \
             mock.patch("dashboard.core.notable_events.record",
                        side_effect=lambda msg, level="info": recorded.append((msg, level))):
            store.cache_set("reconcile_had_mismatch", True)   # simulate a PRIOR mismatch
            result = reconcile.reconcile_with_broker()

        check("current check itself is clean", result, {"only_local": [], "only_broker": []})
        check("exactly one event recorded (the follow-up)", len(recorded), 1)
        check("follow-up message says CLEARED", "CLEARED" in recorded[0][0], True)
        check("follow-up pushed at warning level (reaches Telegram)", recorded[0][1], "warning")
        had, _ = store.cache_get("reconcile_had_mismatch")
        check("cached state updated to False (clean)", had, False)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_reconcile_with_broker_no_followup_when_already_clean():
    print("\nreconcile_with_broker(): a clean check with NO prior mismatch records nothing "
          "extra -- avoids spamming an 'all good' event on every routine reconnect:")
    from dashboard.core import reconcile
    from dashboard.execution import ib_exec

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        recorded = []
        with mock.patch.object(ib_exec, "mirrored_open_symbols", return_value=set()), \
             mock.patch("dashboard.data.ib_client.broker_positions", return_value={}), \
             mock.patch("dashboard.data.ib_client.broker_open_order_symbols", return_value=set()), \
             mock.patch("dashboard.core.notable_events.record",
                        side_effect=lambda msg, level="info": recorded.append((msg, level))):
            result = reconcile.reconcile_with_broker()   # no prior cache entry -> defaults clean

        check("clean result", result, {"only_local": [], "only_broker": []})
        check("no event recorded", len(recorded), 0)
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
    for t in (test_compare_positions_clean_match, test_compare_positions_ghost_trade,
              test_compare_positions_untracked_broker_position,
              test_compare_positions_zero_qty_excluded, test_compare_positions_mixed,
              test_compare_positions_empty_both,
              test_compare_positions_pending_order_not_a_ghost,
              test_compare_positions_pending_order_mixed_with_real_ghost,
              test_compare_positions_no_pending_arg_unchanged,
              test_compare_positions_excludes_cash_sweep_holding,
              test_compare_positions_excluded_does_not_hide_a_real_ghost,
              test_compare_positions_no_excluded_arg_unchanged,
              test_mirrored_open_symbols_isolated_db,
              test_reconcile_with_broker_records_cleared_after_previous_mismatch,
              test_reconcile_with_broker_no_followup_when_already_clean):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
