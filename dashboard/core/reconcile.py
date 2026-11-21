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


QTY_TOLERANCE = 0.5        # sub-share float noise, not a real divergence


def compare_positions(broker_positions: dict[str, float], local_open_symbols: set[str],
                      broker_pending_symbols: set[str] | None = None,
                      excluded_symbols: set[str] | None = None,
                      local_open_qty: dict[str, float] | None = None) -> dict:
    """PURE function (no I/O -- unit-testable in isolation): compare broker's actual non-zero
    positions against the set of instrument symbols this dashboard locally thinks are OPEN.

    Returns {"only_local": [...], "only_broker": [...]}, both sorted lists of symbols.
    - only_local: we have an OPEN trade record, but the broker reports NO position -- a
      "ghost" trade (e.g. the broker filled a close that our mirror bookkeeping missed).
    - only_broker: the broker holds a position we have no local OPEN record for at all (e.g.
      an order placed outside this dashboard, or a local record that got lost/corrupted).
    Either list being non-empty is a real desync worth surfacing -- neither should ever happen
    in normal operation.

    `broker_pending_symbols` (FIXED 2026-07-13): symbols with a live, not-yet-filled order at
    the broker (from ib_client.broker_open_order_symbols()). A local "OPEN" trade record means
    "an order was placed for this," NOT "the broker confirms a filled position" -- those are
    different things, and conflating them produced a false "ghost" alarm for 6 real, correctly
    -placed GTC MKT orders that simply hadn't filled yet (placed outside market hours). A
    symbol with no broker POSITION but a live broker ORDER is exactly the expected state for a
    pending entry, not a desync -- excluded from only_local accordingly.

    `excluded_symbols` (FIXED 2026-07-18): intentional NON-strategy broker holdings that are
    never added to ib_mirror by design -- currently just the cash-sweep's SGOV shield (see
    ib_exec.py's SGOV_SYMBOL) -- so they'd otherwise ALWAYS false-positive as "only_broker
    (untracked)" every single time the sweep holds any shares. Confirmed live 2026-07-18: this
    fired a real "position MISMATCH" alarm for exactly SGOV, self-resolved 6 minutes later once
    the sweep's own next cycle happened to true it back up -- not a real desync, a permanently
    missing exclusion for an intentional holding."""
    broker_symbols = {sym for sym, qty in broker_positions.items() if qty != 0}
    pending = broker_pending_symbols or set()
    excluded = excluded_symbols or set()
    out = {
        "only_local": sorted(local_open_symbols - broker_symbols - pending),
        "only_broker": sorted(broker_symbols - local_open_symbols - excluded),
        "qty_mismatch": [],
    }
    # `local_open_qty` (ADDED 2026-09-05): the set comparison above answers "does this symbol
    # exist on both sides", which says nothing about HOW MUCH. Measured on DUK968178: broker
    # 652 AMLP vs 37 mirrored, broker 453 CPER vs 293 mirrored -- both symbols present on both
    # sides, so the two lists above were empty and reconcile logged a clean match while ~775
    # real shares were untracked and unprotected. Compared only for symbols present on BOTH
    # sides; a symbol missing from one side is already reported by only_local/only_broker and
    # would just double-report here.
    if local_open_qty is not None:
        for sym in sorted(broker_symbols & set(local_open_qty) - excluded):
            b, l = float(broker_positions.get(sym, 0.0)), float(local_open_qty.get(sym, 0.0))
            if abs(b - l) > QTY_TOLERANCE:
                out["qty_mismatch"].append({"symbol": sym, "broker": b, "local": l,
                                            "delta": b - l})
    return out


# ---- long-only guard, phase 1 (IBKR_LONG_ONLY_GUARD_SPEC G7, 2026-11-21) ------------------
# Incident: LIVE sat net short (QQQ -8, AMLP -21, CPER -84, SPY -4, ~31% NAV) for 22 days while
# this module logged a YELLOW mismatch every 10 min and the Telegram text said the reverse of
# the truth. A long-only book must treat ANY negative broker position as a RED incident, and a
# desync must not be able to stay quiet for days.

DIGEST_AFTER_SEC = 24 * 3600     # a mismatch older than this re-pushes a RED digest ...
DIGEST_EVERY_SEC = 24 * 3600     # ... at most once per this interval


def short_positions(broker_positions: dict[str, float],
                    excluded_symbols: set[str] | None = None) -> list[dict]:
    """PURE: every NEGATIVE broker position, as [{"symbol", "qty"}] sorted by symbol.
    This strategy is long-only (paper.LONG_ONLY), so each entry is unintended by definition
    -- regardless of whether the symbol also has a journal row (QQQ #79 did: long 7 in the
    journal, -8 at the broker)."""
    excluded = excluded_symbols or set()
    return [{"symbol": s, "qty": float(q)} for s, q in sorted(broker_positions.items())
            if q is not None and float(q) < -QTY_TOLERANCE and s not in excluded]


def untracked_positions(items: list[dict], tracked_symbols: set[str],
                        excluded_symbols: set[str] | None = None) -> list[dict]:
    """PURE: broker portfolio rows the strategy does not own -- not an OPEN ib_mirror symbol
    and not an intentional holding (the SGOV shield) -- PLUS any short, even on a tracked
    symbol (a short is never a strategy position). Shorts first, then by |market value|.
    `items`: dicts with at least symbol/qty/market_value (see ib_client.broker_portfolio_items)."""
    excluded = excluded_symbols or set()
    out = []
    for it in items or []:
        sym, qty = it.get("symbol"), float(it.get("qty") or 0.0)
        if not sym or abs(qty) <= QTY_TOLERANCE or sym in excluded:
            continue
        if sym in tracked_symbols and qty > 0:
            continue
        out.append(dict(it))
    out.sort(key=lambda r: (0 if float(r.get("qty") or 0) < 0 else 1,
                            -abs(float(r.get("market_value") or 0.0))))
    return out


def mismatch_message(result: dict) -> str:
    """PURE: the canonical one-line mismatch message (parsed by notable_events.humanize)."""
    qty_txt = ", ".join(f"{m['symbol']} broker={m['broker']:.0f} local={m['local']:.0f}"
                        for m in result.get("qty_mismatch") or [])
    short_txt = ", ".join(f"{s['symbol']} {s['qty']:.0f}" for s in result.get("short") or [])
    return ("reconcile: broker/local position MISMATCH -- "
            + (f"SHORT=[{short_txt}] " if short_txt else "")
            + f"only_local(ghost)={result.get('only_local') or []} "
            + f"only_broker(untracked)={result.get('only_broker') or []}"
            + (f" qty_mismatch=[{qty_txt}]" if qty_txt else ""))


def mismatch_level(result: dict) -> str:
    """PURE: "error" (RED tier -> pushes immediately) when the broker holds any short;
    "warning" otherwise (notable_events escalates a repeating warning to RED itself)."""
    return "error" if result.get("short") else "warning"


def digest_due(since_ts: float | None, last_digest_ts: float | None, now: float) -> bool:
    """PURE: should a persisting mismatch re-push a RED digest now?"""
    if not since_ts or now - since_ts < DIGEST_AFTER_SEC:
        return False
    return not last_digest_ts or now - last_digest_ts >= DIGEST_EVERY_SEC


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
    # FIXED 2026-07-13: a local "OPEN" trade with no broker POSITION isn't necessarily a
    # ghost -- it might just be a real order still pending fill (confirmed live: 6 GTC MKT
    # orders placed before the US market opened sat correctly "Submitted" for hours, with
    # zero executions, and were flagged as ghosts the whole time). Check broker open orders
    # too before concluding a real desync.
    broker_pending = ib_client.broker_open_order_symbols()
    result = compare_positions(broker_pos, local_open, broker_pending,
                               excluded_symbols={ib_exec.SGOV_SYMBOL},
                               local_open_qty=ib_exec.mirrored_open_qty())
    result["short"] = short_positions(broker_pos, excluded_symbols={ib_exec.SGOV_SYMBOL})
    from dashboard.core import store
    had_mismatch, _ts = store.cache_get("reconcile_had_mismatch")
    is_mismatch = bool(result["only_local"] or result["only_broker"]
                       or result["qty_mismatch"] or result["short"])
    now = time.time()
    if is_mismatch:
        msg = mismatch_message(result)
        log.warning(msg)
        from dashboard.core import notable_events
        notable_events.record(msg, level=mismatch_level(result))
        # G7.3: a desync must not stay quiet for days (the QQQ short sat YELLOW for 22).
        since, _ = store.cache_get("reconcile_mismatch_since")
        if not since:
            since = now
            store.cache_set("reconcile_mismatch_since", since)
        last_digest, _ = store.cache_get("reconcile_digest_last")
        if digest_due(since, last_digest, now):
            days = (now - since) / 86400.0
            notable_events.record(f"reconcile: mismatch PERSISTING for {days:.1f} days -- {msg}",
                                  level="error", force_push=True)
            store.cache_set("reconcile_digest_last", now)
        result["mismatch_since"] = since
    else:
        store.cache_set("reconcile_mismatch_since", None)
        store.cache_set("reconcile_digest_last", None)
        log.info("reconcile: broker/local positions match (%d open)", len(local_open))
        # RESOLVED 2026-07-21: this check only runs once per FRESH connection, and the
        # "Recent notable events" panel (app.py) just lists the last 20 changelog rows with
        # no resolved/unresolved distinction -- a real mismatch (CWB's ghost entry) stayed
        # visible there for hours after being fixed, with nothing to tell a viewer it was
        # no longer active (a user saw the stale warning and asked whether it was still a
        # live problem). If the PREVIOUS check found a real desync, record the follow-up
        # too (warning level, so it also reaches Telegram as a definitive "all clear",
        # matching how the original alarm was pushed) so the changelog shows resolution,
        # not just a dangling alarm that looks unresolved forever.
        if had_mismatch:
            clear_msg = "reconcile: mismatch CLEARED -- broker/local positions now match"
            from dashboard.core import notable_events
            notable_events.record(clear_msg, level="warning")
    store.cache_set("reconcile_had_mismatch", is_mismatch)
    return result
