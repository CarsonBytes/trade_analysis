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


# ---- broker identity / status (for the UI + retrospective) -----------------

def is_ib() -> bool:
    return os.environ.get("BROKER", "mt5").lower() == "ib"


def name() -> str:
    """Human label for the active broker/account type."""
    return "IBKR Paper" if is_ib() else "MT5 Demo"


def mirror_table() -> str:
    """The sqlite table the active backend records its mirrored orders in."""
    return "ib_mirror" if is_ib() else "mt5_mirror"


def executed_ids() -> set:
    """paper_ids the active broker actually placed (have a mirror row) -- the
    'broker truth' set for the retrospective. Empty set on any error."""
    import sqlite3
    from dashboard.core import paper
    try:
        c = sqlite3.connect(paper._DB)
        ids = {r[0] for r in c.execute(
            f"SELECT paper_id FROM {mirror_table()}").fetchall()}
        c.close()
        return ids
    except Exception:
        return set()


def connection() -> dict:
    """Uniform connection status for the header. Keys: label, ok (on safe acct),
    available (link up), detail (account/server string)."""
    if is_ib():
        from dashboard.data import ib_client
        avail = ib_client.is_available()
        acct = ib_client.account_id() if avail else None
        return {"label": "IBKR", "available": avail, "ok": ib_client.is_paper(),
                "detail": f"acct {acct}" if acct else "gateway down"}
    from dashboard.data import mt5_client
    from dashboard.execution import executor
    cs = mt5_client.connection_status()
    return {"label": "MT5", "available": bool(cs), "ok": executor.is_demo(),
            "detail": (f"{cs['server']} {cs['ping_ms']:.0f}ms" if cs else "not connected")}
