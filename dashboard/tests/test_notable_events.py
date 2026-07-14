"""Unit tests for core/notable_events.py -- the unified changelog + alert hook.
ADDED 2026-07-14.
Run:  uv run python -m dashboard.tests.test_notable_events
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


def test_record_and_recent_isolated_db():
    print("record()/recent(): writes to an isolated temp db, reads back newest-first:")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import notable_events, notify
        with mock.patch.object(notify, "send", return_value=False):
            notable_events.record("first event")
            notable_events.record("second event", level="warning")
        rows = notable_events.recent(limit=10)
        check("2 rows recorded", len(rows), 2)
        check("newest first", rows[0]["message"], "second event")
        check("level recorded correctly", rows[0]["level"], "warning")
        check("default level is info", rows[1]["level"], "info")
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_recent_limit_respected():
    print("\nrecent(limit=N): only returns the N most recent:")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import notable_events, notify
        with mock.patch.object(notify, "send", return_value=False):
            for i in range(5):
                notable_events.record(f"event {i}")
        rows = notable_events.recent(limit=2)
        check("respects the limit", len(rows), 2)
        check("most recent first", rows[0]["message"], "event 4")
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_record_calls_notify():
    print("\nrecord(): forwards to notify.send() with the same message/level:")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import notable_events, notify
        calls = []
        with mock.patch.object(notify, "send",
                              side_effect=lambda msg, level="info": calls.append((msg, level))):
            notable_events.record("halt triggered", level="warning")
        check("notify.send called once", len(calls), 1)
        check("message forwarded correctly", calls[0][0], "halt triggered")
        check("level forwarded correctly", calls[0][1], "warning")
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_record_never_raises_if_notify_fails():
    print("\nrecord(): a notify.send() exception must not propagate (alerting failure "
          "must never break the caller's real trading/monitoring logic):")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import notable_events, notify
        raised = False
        with mock.patch.object(notify, "send", side_effect=RuntimeError("boom")):
            try:
                notable_events.record("something happened")
            except Exception:
                raised = True
        check("no exception propagated", raised, False)
        rows = notable_events.recent(limit=5)
        check("still recorded locally despite notify failing", len(rows), 1)
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
    test_record_and_recent_isolated_db()
    test_recent_limit_respected()
    test_record_calls_notify()
    test_record_never_raises_if_notify_fails()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
