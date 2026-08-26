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
    assert ok, f"{name}: got {got!r} want {want!r}"


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
    print("\nrecord(): PUSH POLICY -- only RED-tier events notify (user-requested "
          "2026-08-26):")
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
            notable_events.record("halt triggered", level="warning")   # red: halt keyword
        check("red event pushes once", len(calls), 1)
        check("push carries error severity", calls[0][1], "error")

        calls.clear()
        with mock.patch.object(notify, "send",
                              side_effect=lambda msg, level="info": calls.append((msg, level))):
            notable_events.record(
                "New order placed: IWM: paper bracket placed BUY 10sh SL 1 TP 3")
        check("routine order-placed does NOT push", len(calls), 0)
        rows = notable_events.recent(limit=5)
        check("but it is still recorded locally", len(rows), 2)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_tiers_dedupe_and_escalation():
    print("\nv3 tiers / dedupe / mismatch escalation:")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import notable_events as ne, notify
        check("error -> red", ne.classify("error", "anything"), "red")
        check("warning -> yellow", ne.classify("warning", "whatever"), "yellow")
        check("info -> white", ne.classify("info", "scan done"), "white")
        check("2FA info escalates -> red", ne.classify("info", "approve 2FA on phone"),
              "red")

        mm = ("reconcile: broker/local position MISMATCH -- "
              "only_local(ghost)=[EIMI] only_broker(untracked)=[]")
        with mock.patch.object(notify, "send", return_value=False):
            ne.record(mm, level="warning")
            ne.record(mm, level="warning")
        rows = ne.recent(limit=10)
        check("duplicate collapses into ONE row (dedupe)", len(rows), 1)
        check("counter incremented", rows[0]["count"], 2)
        check("mismatch starts yellow", rows[0]["tier"], "yellow")
        check("humanized title", rows[0]["title"], "Records disagree (EIMI)")

        calls = []
        with mock.patch.object(notify, "send",
                              side_effect=lambda msg, level="info": calls.append(level)):
            ne.record(mm, level="warning")
        rows = ne.recent(limit=10)
        check(f"escalates to red at {ne.MISMATCH_ESCALATE_AFTER} occurrences",
              rows[0]["tier"], "red")
        check("escalation pushes exactly once", len(calls), 1)

        with mock.patch.object(notify, "send", return_value=False):
            ne.record("reconcile: broker/local position MISMATCH -- "
                      "only_local(ghost)=[CSPX] only_broker(untracked)=[]",
                      level="warning")
        cspx = [r for r in ne.recent(limit=10) if r["symbol"] == "CSPX"]
        eimi = [r for r in ne.recent(limit=10) if r["symbol"] == "EIMI"]
        check("separate symbol = separate incident", len(cspx), 1)
        check("EIMI group untouched by CSPX row", eimi[0]["count"], 3)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_humanize_plain_language():
    print("\nhumanize(): jargon in, plain language out (word-scan acceptance):")
    from dashboard.core import notable_events as ne
    cases = [
        ("reconcile: broker/local position MISMATCH -- only_local(ghost)=[EIMI] "
         "only_broker(untracked)=[]", "Records disagree (EIMI)"),
        ("EIMI: auto-cancelled -- entry never filled at the broker after 31min",
         "EIMI order cancelled"),
        ("New order placed: EIMI: paper bracket placed BUY 64sh SL 52.57 TP 60.17",
         "EIMI buy order sent"),
    ]
    for raw, want_title in cases:
        title, _detail = ne.humanize(raw)
        check(f"{raw[:40]}...", title, want_title)
    banned = ("only_local", "ghost", "perm_id")
    titles = [ne.humanize(c[0])[0] for c in cases]
    ok = not any(b in t.lower() for t in titles for b in banned)
    check("no jargon in titles", ok, True)


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


def test_read_tracking_lifecycle():
    print("\nread tracking (2026-08-26 Alerts-tab spec): new events are UNREAD; only "
          "explicit mark_read()/mark_all_read() change that:")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import notable_events, notify
        with mock.patch.object(notify, "send", return_value=False):
            notable_events.record("event A")
            notable_events.record("event B", level="warning")
        rows = notable_events.recent(limit=10)
        check("fresh events are unread", [r["read"] for r in rows], [False, False])
        check("rows carry their ids for per-row actions",
              sorted(r["id"] for r in rows), [1, 2])
        check("unread_count sees both", notable_events.unread_count(), 2)

        b_id = next(r["id"] for r in rows if r["message"] == "event B")
        notable_events.mark_read(b_id)
        rows = notable_events.recent(limit=10)
        check("mark_read clears exactly the one event",
              {r["message"]: r["read"] for r in rows},
              {"event A": False, "event B": True})
        check("unread_count drops to 1", notable_events.unread_count(), 1)

        # a NEW event arriving afterwards must be unread again -- opening a panel must
        # never have side effects on events recorded after it rendered
        with mock.patch.object(notify, "send", return_value=False):
            notable_events.record("event C")
        check("new event is unread again", notable_events.unread_count(), 2)

        notable_events.mark_all_read()
        rows = notable_events.recent(limit=10)
        check("mark_all_read clears everything",
              all(r["read"] for r in rows), True)
        check("unread_count now zero", notable_events.unread_count(), 0)

        # idempotency: re-marking already-read rows is harmless
        notable_events.mark_read(b_id)
        notable_events.mark_all_read()
        check("re-marking is idempotent", notable_events.unread_count(), 0)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_read_ts_migration_for_pre_existing_db():
    print("\nread_ts migration: a changelog table created BEFORE the column existed "
          "gains it additively, existing rows read as UNREAD:")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        import sqlite3
        import datetime as dt
        # simulate the OLD schema by hand (no read_ts column) with one pre-existing row
        c = sqlite3.connect(path)
        c.execute("""CREATE TABLE changelog (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT)""")
        c.execute("INSERT INTO changelog(ts, level, message) VALUES (?,?,?)",
                  (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "warning", "pre-migration alert"))
        c.commit()
        c.close()

        from dashboard.core import notable_events, notify
        with mock.patch.object(notify, "send", return_value=False):
            notable_events.record("post-migration event")
        rows = notable_events.recent(limit=10)
        check("migration kept the old row",
              any(r["message"] == "pre-migration alert" for r in rows), True)
        check("old rows count as UNREAD (user never saw them)",
              [r["read"] for r in rows], [False, False])
        check("unread_count includes migrated rows", notable_events.unread_count(), 2)
    finally:
        if old is None:
            os.environ.pop("DASH_DB_NAME", None)
        else:
            os.environ["DASH_DB_NAME"] = old
        try:
            os.remove(path)
        except OSError:
            pass


def test_recent_exposes_tier_key():
    print("\nrecent(): REGRESSION for the 2026-08-26 incident -- the COALESCE'd tier column "
          "was aliased 'tier_eff', so the returned dict had NO 'tier' key at all. Every "
          "consumer reads ev['tier'] (alerts_panel's three tier filters), so this was a hard "
          "KeyError -> 500 on the whole dashboard page, confirmed live on paper:")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    try:
        from dashboard.core import notable_events, notify
        with mock.patch.object(notify, "send", return_value=False):
            notable_events.record("a routine thing")
        rows = notable_events.recent(limit=10)
        check("1 row recorded", len(rows), 1)
        check("'tier' key present (the documented contract)", "tier" in rows[0], True)
        check("no leaked SQL alias key", "tier_eff" in rows[0], False)
        check("tier is one of the three real tiers",
              rows[0]["tier"] in ("red", "yellow", "white"), True)
        # the exact expression alerts_panel() uses -- must not raise
        by_tier = [ev for ev in rows if ev["tier"] == "red"]
        check("alerts_panel's own filter expression runs without KeyError",
              isinstance(by_tier, list), True)
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
