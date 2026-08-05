"""Regression test for execution/broker.py's executed_ids(), FIXED 2026-07-13: a VOID mirror
row (a trade we later found out never actually filled/was cancelled at the broker -- see the
historical Error-435 entries and the 2026-07-13 manual ASHR cancellation) used to still count
as "broker truth executed," which made app.py's _pending_reason() wrongly tell the user a
genuinely-dead order was "already placed, waiting to fill." Excluded here at the source.

Run:  uv run python -m dashboard.tests.test_broker
"""
from __future__ import annotations

import os
import tempfile
from unittest import mock

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def test_executed_ids_excludes_void():
    print("executed_ids(): a VOID row must NOT count as broker-executed:")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    old_broker = os.environ.get("BROKER")
    os.environ["DASH_DB_NAME"] = path
    os.environ["BROKER"] = "ib"          # -> mirror_table() picks ib_mirror
    try:
        from dashboard.core import paper
        from dashboard.execution import ib_exec, broker
        with paper._LOCK, ib_exec._conn() as c:
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(1,0,111,'AMLP',6.0,50.0,'','2026-07-08T00:00:00','OPEN','etf')")
            c.execute("INSERT INTO ib_mirror VALUES "
                     "(2,0,222,'ASHR',8.0,60.0,'','2026-07-13T00:00:00','VOID','manually cancelled')")
        ids = broker.executed_ids()
        check("OPEN row's paper_id included", 1 in ids, True)
        check("VOID row's paper_id excluded", 2 in ids, False)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        if old_broker is None:
            os.environ.pop("BROKER", None)
        else:
            os.environ["BROKER"] = old_broker
        try:
            os.remove(path)
        except OSError:
            pass


def test_portfolio_room_usd_dispatch():
    print("\nbroker.portfolio_room_usd(): dispatches to the backend when supported, "
          "None otherwise:")
    from dashboard.execution import broker

    class _FakeBackendWithRoom:
        def current_portfolio_room_usd(self):
            return 1234.5

    class _FakeBackendWithout:
        pass

    with mock.patch.object(broker, "_backend", return_value=_FakeBackendWithRoom()):
        check("returns the backend's value", broker.portfolio_room_usd(), 1234.5)
    with mock.patch.object(broker, "_backend", return_value=_FakeBackendWithout()):
        check("backend without support -> None", broker.portfolio_room_usd(), None)


def test_mirror_qty_column_matches_the_active_backend():
    print("mirror_qty_column(): picks the real per-trade quantity column name for whichever "
          "backend is active -- ib_mirror calls it 'qty', mt5_mirror calls the same concept "
          "'volume' -- used by app.py's Active Trades 'invested' line to read the REAL "
          "broker-requested size for a specific trade (added 2026-08-06 after a user "
          "correctly flagged the previous figure, sized against a fixed $10,000 backtest-"
          "reference account, as misleading once labelled 'invested'):")
    from dashboard.execution import broker
    old_broker = os.environ.get("BROKER")
    old_live = os.environ.get("IB_ALLOW_LIVE")
    try:
        os.environ["BROKER"] = "ib"
        check("IB backend -> 'qty' (matches ib_mirror's column)",
              broker.mirror_qty_column(), "qty")
        os.environ["BROKER"] = "mt5"
        os.environ.pop("IB_ALLOW_LIVE", None)
        check("MT5 backend -> 'volume' (matches mt5_mirror's column)",
              broker.mirror_qty_column(), "volume")
    finally:
        for k, v in (("BROKER", old_broker), ("IB_ALLOW_LIVE", old_live)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
