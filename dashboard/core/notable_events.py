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
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT)""")
        _table_ready.add(db_path)
    return c


def record(message: str, level: str = "info") -> None:
    """Log + record a notable event locally, and alert (Telegram) if configured. Never
    raises -- a failure in the changelog write or the alert must not break whatever real
    trading/monitoring logic triggered this."""
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
    """Most recent notable events, newest first."""
    try:
        with _conn() as c:
            cur = c.execute(
                "SELECT ts, level, message FROM changelog ORDER BY id DESC LIMIT ?", (limit,))
            return [{"ts": r[0], "level": r[1], "message": r[2]} for r in cur.fetchall()]
    except Exception as e:                       # noqa: BLE001
        log.debug("notable_events: recent() read failed: %s", e)
        return []
