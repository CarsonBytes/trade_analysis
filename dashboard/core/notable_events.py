"""Unified 'something notable happened' hook -- ADDED 2026-07-14. One call site per event
type feeds BOTH the local changelog (queried by the UI's Recent Changes panel) and a
Telegram alert (core/notify.py) if configured, so the two features can't drift out of sync
by having their own separate event-detection logic.

Uses the SAME per-instance database as everything else (paper._DB) -- paper and live each
get their own changelog, matching how every other table in this project is already scoped
per-instance.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from dashboard.core.log import log


_table_ready: set[str] = set()   # DB paths already confirmed to have the table -- avoids
                                 # re-running CREATE TABLE IF NOT EXISTS on every single
                                 # call (found 2026-07-14: recent() runs on every dashboard
                                 # render via retrospective_panel(), so a schema-touching
                                 # statement on every read is real, avoidable overhead in a
                                 # hot path, not just a style nit)


def _conn() -> sqlite3.Connection:
    from dashboard.core import paper
    db_path = str(paper._DB)
    c = sqlite3.connect(db_path, check_same_thread=False)
    if db_path not in _table_ready:
        c.execute("""CREATE TABLE IF NOT EXISTS changelog (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT,
            read_ts TEXT DEFAULT NULL)""")
        # ADDED 2026-08-26 (Alerts-tab spec): per-event read tracking. Additive migration
        # for pre-existing DBs -- same pattern as paper.py's own _MIGRATIONS. Unread is
        # simply read_ts IS NULL; NOTHING in the system auto-marks events read (the old
        # bell dialog's open-everything-as-read behavior was found live to dismiss real
        # alerts the user never saw).
        cols = [r[1] for r in c.execute("PRAGMA table_info(changelog)").fetchall()]
        if "read_ts" not in cols:
            c.execute("ALTER TABLE changelog ADD COLUMN read_ts TEXT DEFAULT NULL")
        _table_ready.add(db_path)
    return c


def record(message: str, level: str = "info") -> None:
    """Log + record a notable event locally (as UNREAD), and alert (Telegram/ntfy) if
    configured. Never raises -- a failure in the changelog write or the alert must not
    break whatever real trading/monitoring logic triggered this. New events are always
    unread by definition; only explicit mark_read()/mark_all_read() calls clear them."""
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        from dashboard.core import paper
        with paper._LOCK, _conn() as c:
            c.execute("INSERT INTO changelog(ts, level, message) VALUES (?,?,?)",
                      (ts, level, message))
    except Exception as e:                      # noqa: BLE001
        log.debug("notable_events: changelog write failed: %s", e)
    (log.warning if level in ("warning", "error") else log.info)("EVENT: %s", message)
    try:
        from dashboard.core import notify
        notify.send(message, level=level)
    except Exception as e:                      # noqa: BLE001
        log.debug("notable_events: notify failed: %s", e)


def recent(limit: int = 20) -> list[dict]:
    """Most recent notable events, newest first. Each row carries its id and a 'read'
    flag (read_ts IS NOT NULL) so UIs can distinguish seen from unread."""
    try:
        with _conn() as c:
            cur = c.execute(
                "SELECT id, ts, level, message, read_ts FROM changelog "
                "ORDER BY id DESC LIMIT ?", (limit,))
            return [{"id": r[0], "ts": r[1], "level": r[2], "message": r[3],
                     "read": r[4] is not None} for r in cur.fetchall()]
    except Exception as e:                       # noqa: BLE001
        log.debug("notable_events: recent() read failed: %s", e)
        return []


def unread_count() -> int:
    """How many recorded events have never been marked read. Uncapped -- callers decide
    how to display large counts."""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM changelog WHERE read_ts IS NULL").fetchone()
            return int(row[0]) if row else 0
    except Exception as e:                       # noqa: BLE001
        log.debug("notable_events: unread_count() failed: %s", e)
        return 0


def mark_read(event_id: int) -> None:
    """Mark ONE event read by id. Never raises -- a failed write must not break the UI."""
    try:
        from dashboard.core import paper
        with paper._LOCK, _conn() as c:
            c.execute("UPDATE changelog SET read_ts = ? "
                      "WHERE id = ? AND read_ts IS NULL",
                      (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       event_id))
    except Exception as e:                       # noqa: BLE001
        log.debug("notable_events: mark_read(%s) failed: %s", event_id, e)


def mark_all_read() -> None:
    """Explicitly mark EVERY event read -- a deliberate user action on the Alerts tab,
    never something the system does as a side effect of opening anything."""
    try:
        from dashboard.core import paper
        with paper._LOCK, _conn() as c:
            c.execute("UPDATE changelog SET read_ts = ? WHERE read_ts IS NULL",
                      (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),))
    except Exception as e:                       # noqa: BLE001
        log.debug("notable_events: mark_all_read() failed: %s", e)
