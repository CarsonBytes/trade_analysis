"""Broker reconciliation: on every FRESH gateway connection (login/reconnect, not every
refresh cycle), compare IBKR's actual reported positions against what this dashboard's local
records think is open. Catches the "ghost mirror" bug class this project has already hit once
(a trade marked OPEN locally with no corresponding broker position, or vice versa) -- this is
the realistic, buildable version of "check against IBKR on login": the TWS API has no simple
"give me historical NAV" call (that needs Flex Queries / the Client Portal report API, a
separate, heavier integration), but reqPositions() gives an authoritative CURRENT snapshot that
can be diffed against local state cheaply and reliably.
"""
from __future__ import annotations

from dashboard.core.log import log


def compare_positions(broker_positions: dict[str, float], local_open_symbols: set[str]) -> dict:
    """PURE function (no I/O -- unit-testable in isolation): compare broker's actual non-zero
    positions against the set of instrument symbols this dashboard locally thinks are OPEN.

    Returns {"only_local": [...], "only_broker": [...]}, both sorted lists of symbols.
    - only_local: we have an OPEN trade record, but the broker reports NO position -- a
      "ghost" trade (e.g. the broker filled a close that our mirror bookkeeping missed).
    - only_broker: the broker holds a position we have no local OPEN record for at all (e.g.
      an order placed outside this dashboard, or a local record that got lost/corrupted).
    Either list being non-empty is a real desync worth surfacing -- neither should ever happen
    in normal operation."""
    broker_symbols = {sym for sym, qty in broker_positions.items() if qty != 0}
    return {
        "only_local": sorted(local_open_symbols - broker_symbols),
        "only_broker": sorted(broker_symbols - local_open_symbols),
    }


def reconcile_with_broker() -> dict:
    """I/O wrapper: fetch broker positions + local OPEN trades, compare, log any mismatch.
    Called once per FRESH connection (gated by ib_client.reconcile_needed()), not every
    refresh cycle -- this is a login-time consistency check, not a live feed."""
    import time
    from dashboard.data import ib_client
    from dashboard.execution import ib_exec

    # ib_mirror (status='OPEN'), NOT paper.all_trades() -- the paper journal tracks
    # signal/idea state (a trade can be status=OPEN there without ever having been
    # placed/filled at the broker); ib_mirror is what actually got sent to IBKR.
    local_open = ib_exec.mirrored_open_symbols()

    broker_pos = ib_client.broker_positions()
    if broker_pos is None:
        return {"skipped": "broker unavailable"}
    # Right after a fresh connect, IBKR's positionEnd can fire before the account's
    # position snapshot has actually synced, so reqPositionsAsync() can legitimately
    # come back empty even with real positions open (seen live: reconcile ran ~10s after
    # connect and still reported all 7 real positions as "ghosts" -- live has two managed
    # accounts under one login (U12991898 real + an empty U20738951), and a 6s retry
    # budget wasn't consistently enough for the real account's data to land). This runs
    # once per fresh connect on a background thread, so a slower, more generous retry
    # budget costs nothing -- if the broker reports nothing but we expect open positions,
    # keep retrying for up to ~24s before concluding it's a real mismatch.
    if not broker_pos and local_open:
        for _ in range(8):
            time.sleep(3.0)
            retry = ib_client.broker_positions()
            if retry:
                broker_pos = retry
                break
    result = compare_positions(broker_pos, local_open)
    if result["only_local"] or result["only_broker"]:
        log.warning("reconcile: broker/local position MISMATCH on login -- "
                    "only_local(ghost)=%s only_broker(untracked)=%s",
                    result["only_local"], result["only_broker"])
    else:
        log.info("reconcile: broker/local positions match (%d open)", len(local_open))
    return result
