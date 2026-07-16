"""Tiny SQLite store. Its main job is the DAILY API-CALL BUDGET GUARD, which
must survive restarts so the 200/day cap is actually enforced (not reset every
time you relaunch). Also caches the last board-scan so a restart shows data
immediately.
"""
from __future__ import annotations

import json
import os
import sqlite3
import datetime as dt
import pathlib
import threading

# stable dashboard/ root (store.py is in dashboard/core/) -- same db as paper._DB.
# DASH_DB_NAME picks PAPER vs LIVE database so switching modes never mixes trade history/
# journal/settings between accounts. `_DB` is LAZY (via module __getattr__ below), computed
# fresh from the env var on every access -- NOT fixed at import time -- because `store` itself
# is imported early (to resolve the mode) before DASH_DB_NAME is necessarily set.
_LOCK = threading.Lock()


def _dbpath() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / os.environ.get("DASH_DB_NAME", "dashboard.db")


def __getattr__(name):     # PEP 562: makes `store._DB` a live property, not an import-time constant
    if name == "_DB":
        return _dbpath()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# The MODE POINTER itself must live in an ALWAYS-FIXED file, independent of DASH_DB_NAME --
# otherwise there's a chicken-and-egg problem (you'd need to already know the mode to find the
# file that tells you the mode). get_mode()/set_mode() are the only functions using _MODE_DB.
_MODE_DB = pathlib.Path(__file__).resolve().parents[1] / "dashboard_mode.db"


def get_mode() -> str:
    """Read the persisted paper/live mode. ALWAYS in the same fixed file (see _MODE_DB).
    Safe to call before DASH_DB_NAME is set -- must be called first, at app startup."""
    with _LOCK, sqlite3.connect(_MODE_DB, check_same_thread=False) as c:
        c.execute("CREATE TABLE IF NOT EXISTS mode(k TEXT PRIMARY KEY, v TEXT)")
        row = c.execute("SELECT v FROM mode WHERE k='dash_mode'").fetchone()
        return row[0] if row else "paper"


def set_mode(mode: str) -> None:
    with _LOCK, sqlite3.connect(_MODE_DB, check_same_thread=False) as c:
        c.execute("CREATE TABLE IF NOT EXISTS mode(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO mode(k,v) VALUES('dash_mode',?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (mode,))


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_dbpath(), check_same_thread=False)
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
    reserve kept for manual refreshes).

    Checks BOTH this instance's own local count AND the cross-project shared
    total (paper + live + study + event-radar all draw from the SAME
    chatanywhere.tech key -- see reference-shared-llm-ledger). A purely local
    check lets paper and live each independently accumulate up to `cap` before
    either notices, i.e. ~2x the real shared cap before anything blocks.
    Confirmed live 2026-07-15: local counters read 76/200 (paper) and 75/200
    (live) -- both individually "fine" -- while the real combined key usage
    across all four consumers was already past 200 for the day.

    The shared check fails toward BLOCKING (not calling) if the shared fetch
    itself fails/is unreachable -- skipping one board-scan cycle costs
    nothing (risk_gate is fully deterministic regardless of the LLM signal),
    so a conservative default here is free; silently trusting a failed fetch
    as "0 calls, all clear" would not be."""
    if calls_today() >= (cap - reserve):
        return False
    from analyst import usage_log
    ok, shared_calls = usage_log.shared_calls_ok(cap=cap, reserve=reserve)
    return ok


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
