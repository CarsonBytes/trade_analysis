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


def keep_cash_usd() -> dict:
    """Convert idle HKD cash to USD (clears USD margin debit; IB only; CASH_USD=1)."""
    b = _backend()
    if hasattr(b, "keep_cash_usd"):
        return b.keep_cash_usd()
    return {"enabled": False}


def sweep_cash() -> dict:
    """IDLE-CASH sweep into SGOV (IB backend only; opt-in via CASH_SWEEP=1)."""
    b = _backend()
    if hasattr(b, "sweep_cash"):
        return b.sweep_cash()
    return {"enabled": False}


def prepare_withdrawal(amount_usd: float, dry_run: bool = False) -> dict:
    """Free cash for a manual withdrawal from the cash shield (idle USD -> SGOV) FIRST,
    never the Core book; earmarks a reserve the sweep respects. IB only. Does NOT move
    money out (manual IBKR action by design)."""
    b = _backend()
    if hasattr(b, "prepare_withdrawal"):
        return b.prepare_withdrawal(amount_usd, dry_run=dry_run)
    return {"ready": False, "log": "withdrawal helper is IB-only"}


def clear_withdraw_reserve() -> None:
    """Clear the withdrawal reserve after you've withdrawn in IBKR (IB only)."""
    b = _backend()
    if hasattr(b, "clear_withdraw_reserve"):
        b.clear_withdraw_reserve()


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


def account_summary() -> dict | None:
    """Active-broker account balances for the header. IBKR: NetLiquidation/cash/
    buying-power/PnL. MT5: balance/equity. None if unavailable."""
    if is_ib():
        from dashboard.data import ib_client
        return ib_client.account_summary()
    from dashboard.data import mt5_client
    try:
        info = mt5_client.account_info()           # may not exist on all builds
    except Exception:
        info = None
    if not info:
        return None
    return {"NetLiquidation": getattr(info, "equity", None),
            "TotalCashValue": getattr(info, "balance", None)}


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
