"""Unit tests for broker reconciliation:
  - compare_positions() (core/reconcile.py) -- pure, no I/O.
  - mirrored_open_symbols() (execution/ib_exec.py) -- reads ib_mirror, isolated here
    against a throwaway temp sqlite db (never touches the real paper/live journal).
Run:  uv run python -m dashboard.tests.test_reconcile
"""
from __future__ import annotations

import os
import tempfile

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


if __name__ == "__main__":
    for t in (test_compare_positions_clean_match, test_compare_positions_ghost_trade,
              test_compare_positions_untracked_broker_position,
              test_compare_positions_zero_qty_excluded, test_compare_positions_mixed,
              test_compare_positions_empty_both, test_mirrored_open_symbols_isolated_db):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
