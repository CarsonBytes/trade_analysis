"""Global cancel + re-arm -- the manual escalation for orphan orders this client can't cancel.

WHY (IBKR_LONG_ONLY_GUARD_SPEC §3.3, guard phase 3, 2026-11-21): ib.cancelOrder() only works
from the clientId that placed an order (Error 10147 otherwise). The orphan stops that opened the
2026-10/11 LIVE shorts (AMLP -21, CPER -84, SPY -4) were exactly that kind: placed by an earlier
client, left resting after their trades were marked VOID. The automatic sweeper
(ib_exec.sweep_orphan_orders) raises RED when it can't remove one; this tool is the fix:
reqGlobalCancel() cancels EVERY open order on the account regardless of client, then -- once the
broker shows no resting orders on two consecutive reads -- re-arms protection for every real
position through the normal, guarded path (ib_exec.reprotect_naked_positions()).

SAFETY:
  * DRY RUN unless APPLY=1: lists every resting order (all clients) and what would be cancelled.
  * Global cancel also removes the strategy's OWN protective brackets for a moment. The re-arm
    puts them back (sized from the BROKER position through place_guarded); positions are
    unprotected in between, so run it while the market is open and watch the output.
  * Pending cash-sweep / entry orders are cancelled too and simply retry on later cycles.

Run (inside the container):
    docker exec -w /app -e PYTHONPATH=/app -e IB_CLIENT_ID=89 quant-dashboard-live-docker \\
        /app/.venv/bin/python -m dashboard.ops.global_cancel_rearm
    ... then the same with -e APPLY=1 to actually do it.
"""
from __future__ import annotations

import os
import time

from dashboard.core.mode import resolve_mode

resolve_mode()

from dashboard.core import store                      # noqa: E402
from dashboard.data import ib_client                  # noqa: E402
from dashboard.execution import broker, ib_exec       # noqa: E402

APPLY = os.environ.get("APPLY") == "1"
READ_GAP_SEC = 30
MAX_WAIT_SEC = 300


def _open_orders(ib) -> list:
    async def _req():
        return await ib.reqAllOpenOrdersAsync()
    return list(ib_client._run(_req(), timeout=20) or [])


def main() -> None:
    ib = ib_exec._guard()
    if ib is None:
        raise SystemExit("no usable IB connection (see ib_exec._guard)")
    acct = ib_client.account_id()
    print(f"account: {acct}   {'LIVE -- REAL MONEY' if broker.is_live() else 'PAPER'}   "
          f"MODE: {'APPLY' if APPLY else 'DRY RUN'}")
    orders = _open_orders(ib)
    positions = ib_client.call(lambda: {p.contract.conId: float(p.position) for p in
                                        ib_client.filter_by_account(ib.positions() or [], acct)})
    from dashboard.execution.ib_exec import _conn, classify_open_orders
    with _conn() as c:
        mstat = dict(c.execute("SELECT paper_id, status FROM ib_mirror").fetchall())
    rows = classify_open_orders(orders, mstat, positions)
    print(f"\n{len(rows)} resting order(s) across all API clients:")
    for r in rows:
        print(f"  {r['class']:20} {r['symbol']:6} {r['side']:4} {r['qty']:>8g} {r['type']:4} "
              f"ref={r['ref'] or '-'}")
    if not rows:
        print("nothing resting -- nothing to cancel.")
        return
    if not APPLY:
        print("\nDRY RUN: APPLY=1 would send reqGlobalCancel() (cancels ALL of the above), wait "
              "for two clean reads, then re-arm protection for every real position.")
        return
    print("\nsending reqGlobalCancel() ...")
    ib_client.call(lambda: ib.reqGlobalCancel(), timeout=15)
    t0, clean = time.time(), 0
    while time.time() - t0 < MAX_WAIT_SEC:
        time.sleep(READ_GAP_SEC)
        left = _open_orders(ib)
        clean = clean + 1 if not left else 0
        print(f"  read: {len(left)} order(s) still resting (clean reads in a row: {clean})")
        if clean >= 2:
            break
    if clean < 2:
        print("!! orders still resting after the wait -- NOT re-arming; check TWS.")
        return
    # Two clean reads ARE the confirmation reprotect_naked_positions()' zero-order streak guard
    # waits for (it exists for the unsynced-snapshot case), so let it act on this cycle.
    store.cache_set(ib_exec.REPROTECT_ZERO_KEY, ib_exec.REPROTECT_ZERO_CONFIRM - 1)
    logs = ib_exec.reprotect_naked_positions()
    print("\nre-arm:")
    for line in logs or ["(no strategy position needed re-protection)"]:
        print(" ", line)


if __name__ == "__main__":
    main()
