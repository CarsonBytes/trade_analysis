"""Flatten UNINTENDED SHORT positions, in tranches sized to available buying power.

WHY THIS EXISTS (2026-09-01/02): duplicate protective brackets in separate OCA groups
over-sold three paper positions and flipped longs into shorts -- HYD reached -6,402 shares
against a journal record of +66 long, CWB -50 against +54. The bug is fixed
(reprotect_naked_positions' empty-order-list guard, and manual_close_position now sizing
from the real broker position), but the resulting positions still have to be unwound by
hand. See HANDOFF 2026-08-31 / 2026-09-01.

THE INVARIANT THIS RELIES ON: `paper.LONG_ONLY` is True whenever BROKER=ib -- the strategy
never intentionally shorts. So on an IB deployment, any short position is by definition
unintended and flattening it is always the correct direction. The script refuses to run if
LONG_ONLY is False, because then a short might be a real position.

SAFETY PROPERTIES (each enforced below, not merely intended):
  * DRY RUN unless APPLY=1. The dry run prints the exact orders it would send.
  * Only ever BUYS, and only against a position that is genuinely SHORT -- it can reduce
    |position| and nothing else. It can never open, extend, or flip a position long.
  * Every tranche is capped by BOTH the remaining short AND what buying power affords
    (with BP_SAFETY headroom), so it cannot submit an order the account can't cover.
  * Position and buying power are RE-READ from the broker between tranches; the loop trusts
    broker truth each time rather than its own arithmetic.
  * Refuses to run outside US regular trading hours unless FORCE_RTH=1 -- a market order
    into a closed book sits unfilled and confuses the next cycle.
  * Touches no local records. The journal rows for these trades are already resolved, and
    live_positions() drops a position once the broker reports 0, so the dashboard's
    "Flagged positions" clears on its own once the account is genuinely flat.

Run (inside the container):
    docker exec -w /app -e PYTHONPATH=/app -e IB_CLIENT_ID=88 quant-dashboard-docker \
        /app/.venv/bin/python -m dashboard.ops.unwind_shorts
    ... then the same with -e APPLY=1 to actually send.

Env:
    APPLY=1        actually place orders (default: dry run)
    ONLY=HYD,CWB   restrict to these symbols (default: every unintended short)
    MAX_TRANCHES=6 safety stop on the number of orders per symbol (default 6)
    BP_SAFETY=0.85 fraction of buying power a single tranche may consume (default 0.85)
    FORCE_RTH=1    allow running outside US regular trading hours
"""
from __future__ import annotations

import datetime as dt
import os
import time
from zoneinfo import ZoneInfo

from dashboard.core.mode import resolve_mode

resolve_mode()

from dashboard.core import paper                      # noqa: E402
from dashboard.core.log import log                    # noqa: E402
from dashboard.data import ib_client                  # noqa: E402
from dashboard.execution import broker, ib_exec       # noqa: E402

APPLY = os.environ.get("APPLY") == "1"
ONLY = {s.strip().upper() for s in os.environ.get("ONLY", "").split(",") if s.strip()}
MAX_TRANCHES = int(os.environ.get("MAX_TRANCHES", "6"))
BP_SAFETY = float(os.environ.get("BP_SAFETY", "0.85"))
FORCE_RTH = os.environ.get("FORCE_RTH") == "1"
SETTLE_SEC = 12                    # let a market order fill before re-reading the position


def _rth_now() -> bool:
    """US regular trading hours, weekdays 09:30-16:00 ET."""
    now = dt.datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    return dt.time(9, 30) <= now.time() <= dt.time(16, 0)


def _usd_per_base(ccy: str) -> float:
    return ib_client.fx_to_usd(ccy) or ib_client._PEG_USD_PER.get(ccy, 1.0)


def _shorts(ib, acct) -> list:
    """Real short positions, newest broker truth. [(conId, contract, qty, symbol)]"""
    out = []
    for p in ib_client.call(lambda: ib_client.filter_by_account(ib.positions() or [], acct)):
        if p.position < 0 and (not ONLY or p.contract.symbol.upper() in ONLY):
            out.append((p.contract.conId, p.contract, float(p.position), p.contract.symbol))
    return sorted(out, key=lambda r: r[2])              # biggest short first


def _mark(ib, acct, con_id) -> float | None:
    for i in ib_client.call(lambda: ib_client.filter_by_account(ib.portfolio() or [], acct)):
        if i.contract.conId == con_id and i.marketPrice:
            return float(i.marketPrice)
    return None


def _buying_power_usd() -> float:
    acct = ib_client.account_summary() or {}
    bp = acct.get("BuyingPower")
    if bp is None:
        return 0.0
    return float(bp) * _usd_per_base(acct.get("_ccy", ""))


def main() -> None:
    if not paper.LONG_ONLY:
        raise SystemExit("REFUSING: LONG_ONLY is False -- a short may be a real position "
                         "here, so 'every short is unintended' does not hold.")
    ib = ib_exec._guard()
    if ib is None:
        raise SystemExit("no usable IB connection (see ib_exec._guard)")
    acct = ib_client.account_id()
    live = broker.is_live()
    print(f"account: {acct}   {'LIVE -- REAL MONEY' if live else 'PAPER'}   "
          f"MODE: {'APPLY' if APPLY else 'DRY RUN'}")

    if not _rth_now():
        msg = ("US regular trading hours are CLOSED -- a market order would sit unfilled. "
               "Set FORCE_RTH=1 to override.")
        if not FORCE_RTH:
            print(f"\nREFUSING: {msg}")
            print("(the dry run below still shows what WOULD be sent)\n")
        else:
            print(f"\nWARNING: {msg} Proceeding because FORCE_RTH=1.\n")

    shorts = _shorts(ib, acct)
    if not shorts:
        print("\nNo unintended short positions. Nothing to do.")
        return

    print(f"\n{len(shorts)} unintended short position(s):")
    for _cid, _c, qty, sym in shorts:
        print(f"  {sym:<6} SHORT {abs(qty):,.0f}")

    for con_id, contract, qty0, sym in shorts:
        print(f"\n{'=' * 60}\n{sym}: unwinding SHORT {abs(qty0):,.0f}")
        for n in range(1, MAX_TRANCHES + 1):
            # --- re-read broker truth every tranche -------------------------
            cur = next((q for cid, _c, q, _s in _shorts(ib, acct) if cid == con_id), 0.0)
            if cur >= 0:
                print(f"  [{n}] {sym} is no longer short (position {cur:,.0f}) -- done.")
                break
            px = _mark(ib, acct, con_id)
            if not px:
                print(f"  [{n}] no live mark for {sym} -- stopping (retry when quotes flow).")
                break
            bp = _buying_power_usd()
            afford = int((bp * BP_SAFETY) // px)
            want = int(abs(cur))
            take = min(want, afford)
            print(f"  [{n}] short {want:,}  mark ~{px:,.2f}  buying power ${bp:,.0f} "
                  f"-> affords {afford:,}  => BUY {take:,}")
            if take <= 0:
                print("       buying power exhausted -- run again after settlement.")
                break
            if not APPLY:
                print(f"       DRY RUN: would send BUY {take:,} {sym} MKT "
                      f"(~${take * px:,.0f})")
                if take >= want:
                    print("       that single tranche would flatten it.")
                    break
                print("       (dry run cannot simulate the freed margin; "
                      "a real run re-reads buying power and continues)")
                break
            if not _rth_now() and not FORCE_RTH:
                print("       BLOCKED: market closed and FORCE_RTH not set.")
                break

            def _send():
                import ib_async
                o = ib_async.MarketOrder("BUY", take)
                o.tif = "DAY"
                o.orderRef = f"unwind-short#{sym}"
                if acct:
                    o.account = acct
                return ib.placeOrder(contract, o)

            ib_client.call(_send, timeout=20)
            log.warning("unwind_shorts: sent BUY %s %s to reduce an unintended short", take, sym)
            print(f"       SENT BUY {take:,} {sym}; waiting {SETTLE_SEC}s to re-read...")
            time.sleep(SETTLE_SEC)
        else:
            print(f"  reached MAX_TRANCHES={MAX_TRANCHES} for {sym} -- re-run to continue.")

    print(f"\n{'=' * 60}\nfinal broker state:")
    left = _shorts(ib, acct)
    if not left:
        print("  no short positions remain.")
    else:
        for _cid, _c, qty, sym in left:
            print(f"  {sym:<6} STILL SHORT {abs(qty):,.0f}")
    if not APPLY:
        print("\nDRY RUN -- set APPLY=1 to send.")


if __name__ == "__main__":
    main()
