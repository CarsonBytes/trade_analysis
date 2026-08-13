"""A tiny, pure, independently-testable utility extracted from app.py's `_resolve_mode()`
(2026-08-13) so the DASH_DB_NAME-preservation property has an actual regression test --
app.py itself can't be safely imported in a test (it calls `ui.run()` at module level, which
blocks), so the safety-critical logic lives here instead and app.py just calls it. Same
extraction pattern as `resilient_loop.py`.
"""
from __future__ import annotations

import os

from dashboard.core import store


def resolve_mode() -> str:
    """CONCURRENT paper+live: TWO separate long-running processes, each PINNED to one mode via
    DASH_FIXED_MODE (set by its launch script -- dashboard.ps1 sets 'paper', run_dashboard_live.ps1
    sets 'live'). Each has its own port, IB gateway/account, and database (DASH_DB_NAME) -- fully
    isolated, no shared state except the read-only fact of which Cloudflare hostname reaches which.
    (The old single-endpoint restart-switch via store.get_mode()/set_mode() still works as a
    fallback for a process that does NOT set DASH_FIXED_MODE, but concurrent operation should
    always pin it explicitly -- this avoids two processes ever racing on the same shared pointer.)"""
    mode = (os.environ.get("DASH_FIXED_MODE") or store.get_mode() or "paper").lower()
    if mode == "live":                                   # override the paper .env defaults
        os.environ["IB_PORT"] = os.environ.get("LIVE_IB_PORT", "4001")
        os.environ["IB_ACCOUNT"] = os.environ.get("LIVE_IB_ACCOUNT", "U12991898")
        os.environ["IB_ALLOW_LIVE"] = "1"                # arms the ib_exec guard for the live acct
        # setdefault, NOT unconditional assignment (FIXED 2026-08-13): this used to always
        # stomp DASH_DB_NAME to the relative native-deployment default, even when a deployment
        # (e.g. the WSL2/Docker container) had already set it explicitly via its own env --
        # confirmed live: the Docker paper dashboard silently wrote every trade/reconcile/cache
        # update to /app/dashboard/dashboard.db (the container's own throwaway image layer,
        # resolved from this hardcoded relative default) instead of the persistent volume path
        # docker-compose.yml deliberately set, for its ENTIRE runtime -- invisible because every
        # individual read/write round-tripped consistently through the SAME wrong path, so
        # nothing ever errored; only caught by comparing against a fresh diagnostic script that
        # imported paper.py directly (bypassing this function, since it lives in app.py) and
        # saw the correct path. setdefault preserves native behaviour exactly (its launch
        # scripts never set DASH_DB_NAME themselves, so this remains the effective default)
        # while letting an explicit override actually stick.
        os.environ.setdefault("DASH_DB_NAME", "dashboard_live.db")  # SEPARATE journal/history
    else:
        os.environ.pop("IB_ALLOW_LIVE", None)            # paper: guard stays paper-only
        os.environ.setdefault("DASH_DB_NAME", "dashboard.db")  # the original/paper journal
    return mode
