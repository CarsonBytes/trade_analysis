"""Broker dispatch shim: routes execution to the MT5 (executor) or IBKR
(ib_exec) backend based on the BROKER env var. Both backends expose the same
surface, so callers (service.py, the UI) use this instead of importing a
specific backend. "mt5" is the proven default; "ib" switches to futures.

Only the four cycle/read functions service.py needs are dispatched here. Backend
-specific helpers (executor.flatten_foreign, is_demo / is_paper) are still
imported directly where used.
"""
from __future__ import annotations

import os


def _backend():
    if os.environ.get("BROKER", "mt5").lower() == "ib":
        from dashboard.execution import ib_exec
        return ib_exec
    from dashboard.execution import executor
    return executor


def mirror_new() -> list[str]:
    return _backend().mirror_new()


def sync_closures() -> list[str]:
    return _backend().sync_closures()


def live_positions() -> dict:
    return _backend().live_positions()


def reconcile() -> list[dict]:
    return _backend().reconcile()


def account_ok() -> bool:
    """True if the active backend is connected to its safe (demo/paper) account."""
    b = _backend()
    return b.is_paper() if hasattr(b, "is_paper") else b.is_demo()
