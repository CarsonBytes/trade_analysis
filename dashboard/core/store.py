"""Tiny SQLite store. Its main job is the DAILY API-CALL BUDGET GUARD, which
must survive restarts so the 200/day cap is actually enforced (not reset every
time you relaunch). Also caches the last board-scan so a restart shows data
immediately.
"""
from __future__ import annotations

import json
import sqlite3
import datetime as dt
import pathlib
import threading

# stable dashboard/ root (store.py is in dashboard/core/) -- same db as paper._DB
_DB = pathlib.Path(__file__).resolve().parents[1] / "dashboard.db"
_LOCK = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB, check_same_thread=False)
    c.execute("CREATE TABLE IF NOT EXISTS api_usage (day TEXT PRIMARY KEY, count INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, ts TEXT)")
    return c


def _today() -> str:
    return dt.date.today().isoformat()


# ---- API budget guard ------------------------------------------------------

def calls_today() -> int:
    with _LOCK, _conn() as c:
        row = c.execute("SELECT count FROM api_usage WHERE day=?", (_today(),)).fetchone()
        return row[0] if row else 0


def record_call(n: int = 1) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO api_usage(day,count) VALUES(?,?) "
            "ON CONFLICT(day) DO UPDATE SET count=count+?",
            (_today(), n, n),
        )


def can_call(cap: int = 200, reserve: int = 10) -> bool:
    """True if we can spend an LLM call without breaching the cap (minus a small
    reserve kept for manual refreshes)."""
    return calls_today() < (cap - reserve)


# ---- cache (last board scan etc.) -----------------------------------------

def cache_set(key: str, value) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO cache(k,v,ts) VALUES(?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts=excluded.ts",
            (key, json.dumps(value), dt.datetime.now().isoformat(timespec="seconds")),
        )


def cache_get(key: str):
    with _LOCK, _conn() as c:
        row = c.execute("SELECT v, ts FROM cache WHERE k=?", (key,)).fetchone()
        if not row:
            return None, None
        return json.loads(row[0]), row[1]
