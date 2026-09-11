"""Unit tests for the 2026-09-11 fixes behind "why don't flagged positions get auto healed".

Two independent defects, both silent:

1. `ib_client._ensure_conn()`'s clientId-collision fallback was DEAD CODE in the exact case
   it existed for. IB Gateway reports a clientId collision as API error 326 through
   `errorEvent` and then closes the socket, so `connectAsync` raises a bare `TimeoutError()`
   whose `str()` is the EMPTY STRING. The guard `if "client id" in msg or "326" in msg`
   therefore never matched, the loop hit `break` on the first attempt, and base_id+1..+3 were
   never tried. Confirmed live on DUK968178: the gateway leaked clientId 31 internally (no
   TCP session held it -- every attempt was torn down cleanly), and the paper dashboard sat
   disconnected for ~10 HOURS, 124 failed connects in 3h, logging only
   `connect to ib-gateway:4004 failed () -- falling back`.

2. `heal_flagged_positions()` collapsed "the broker is unreachable" (live_positions() -> None)
   and "nothing needs healing" ({}) into the same silent `return []`. So while the connection
   was down the healer did nothing and said nothing, and the dashboard kept rendering flagged
   cards from the last-good cache with no explanation anywhere.

Run:  uv run python -m dashboard.tests.test_heal_visibility
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


# --------------------------------------------------------------------------- 1. clientId

class _FakeIB:
    """Minimal ib_async.IB stand-in that reports a clientId collision the way the real
    gateway does: error 326 on errorEvent, then a bare TimeoutError out of connectAsync."""

    def __init__(self, collide_ids):
        self._collide = collide_ids
        self.handlers = []
        self.errorEvent = self
        self.connected_as = None

    # errorEvent += / -= protocol
    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def __isub__(self, h):
        if h in self.handlers:
            self.handlers.remove(h)
        return self

    def connectAsync(self, host, port, clientId=0, timeout=8, readonly=False):
        if clientId in self._collide:
            for h in list(self.handlers):
                h(-1, 326, "Unable to connect as the client id is already in use.", None)
            raise TimeoutError()            # str() == "" -- the whole point
        self.connected_as = clientId
        return "connected"

    def isConnected(self):
        return self.connected_as is not None

    def reqMarketDataType(self, n):
        pass


def _connect_with(collide_ids, base_id=31):
    from dashboard.data import ib_client
    fake = _FakeIB(collide_ids)
    mod = mock.MagicMock()
    mod.IB.return_value = fake
    env = {"IB_HOST": "gw", "IB_PORT": "4004", "IB_CLIENT_ID": str(base_id)}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(ib_client, "_mod", return_value=mod), \
         mock.patch.object(ib_client, "_run",
                           side_effect=lambda coro, timeout=None: coro), \
         mock.patch.dict(ib_client._S, {"ib": None, "connected": False, "last_attempt": 0.0}):
        ib_client._ensure_conn()
    return fake.connected_as


def test_client_id_collision_falls_through_to_the_next_id():
    print("_ensure_conn(): REGRESSION for the 2026-09-11 outage. A clientId collision arrives "
          "as error 326 on errorEvent followed by a bare TimeoutError() -- whose str() is the "
          "EMPTY STRING, so the old message-parsing guard never matched and the loop broke on "
          "the first attempt. The fallback must actually fall back:")
    check("base id taken -> connects on the next one",
          _connect_with({31}, base_id=31), 32)
    check("three taken -> still finds the fourth",
          _connect_with({31, 32, 33}, base_id=31), 34)
    check("nothing taken -> uses the configured id, unchanged",
          _connect_with(set(), base_id=31), 31)


def test_all_client_ids_taken_gives_up_rather_than_looping():
    print("\n_ensure_conn(): when every id in the band is taken it must return None, not spin:")
    check("all four taken -> no connection", _connect_with({31, 32, 33, 34}, base_id=31), None)


# ------------------------------------------------------------------- 2. heal visibility

def test_broker_unreachable_is_recorded_not_silently_skipped():
    print("\nheal_flagged_positions(): live_positions() returns None when the BROKER IS "
          "UNREACHABLE and {} when genuinely flat. Collapsing both into a bare `return []` is "
          "what made this unanswerable from the dashboard -- the healer no-opped for ~10h "
          "while flagged cards kept rendering from cache. The two must be distinguishable:")
    old, path = _isolated_db()
    try:
        from dashboard.execution import ib_exec
        with mock.patch.object(ib_exec, "live_positions", return_value=None):
            out = ib_exec.heal_flagged_positions()
        check("still returns no actions", out, [])
        st = ib_exec.heal_status()
        check("but the reason is recorded", "broker unreachable" in st["state"], True)
        check("and it is NOT reported as a clean run", st["state"] == "ok", False)
    finally:
        _restore_db(old, path)


def test_genuinely_flat_broker_is_reported_as_a_clean_run():
    print("\nheal_flagged_positions(): an empty (but reachable) broker IS a clean run -- it "
          "must not be confused with the unreachable case above:")
    old, path = _isolated_db()
    try:
        from dashboard.execution import ib_exec
        with mock.patch.object(ib_exec, "live_positions", return_value={}):
            ib_exec.heal_flagged_positions()
        st = ib_exec.heal_status()
        check("recorded as ok", st["state"].startswith("ok"), True)
        check("age is fresh, so the UI does not warn", st["age_sec"] is not None and
              st["age_sec"] < 60, True)
    finally:
        _restore_db(old, path)


def test_heal_status_defaults_to_never_run_on_a_fresh_database():
    print("\nheal_status(): a database where the healer has never run must say so, rather "
          "than looking like a successful run with no work to do:")
    old, path = _isolated_db()
    try:
        from dashboard.execution import ib_exec
        st = ib_exec.heal_status()
        check("state", st["state"], "never run")
        check("age_sec is None (the UI renders this as 'never')", st["age_sec"], None)
        check("no refusals", st["refusals"], {})
    finally:
        _restore_db(old, path)


def test_guard_refusals_are_recorded_per_trade():
    print("\n_note_refusal(): a flagged card must be able to say WHY it was not healed. "
          "Refusals are keyed by paper_id so each card renders its own reason:")
    old, path = _isolated_db()
    try:
        from dashboard.execution import ib_exec
        ib_exec._note_refusal(147, "price 39.8892 is already past its own SL")
        ib_exec._note_refusal(150, "the trade is long but the broker holds a short position")
        st = ib_exec.heal_status()
        check("both recorded", sorted(st["refusals"]), ["147", "150"])
        check("reason survives verbatim",
              st["refusals"]["150"].startswith("the trade is long"), True)
        ib_exec._set_heal_state("ok")
        check("a later clean run keeps the refusals (they are still true)",
              sorted(ib_exec.heal_status()["refusals"]), ["147", "150"])
    finally:
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
