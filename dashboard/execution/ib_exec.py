"""IBKR paper-account auto-execution: mirrors the paper journal to a real IB
PAPER account so forward trades get real futures fills, commissions and rolls.

The futures analog of executor.py (MT5). Same public surface so service.py can
dispatch on BROKER: mirror_new / sync_closures / live_positions / reconcile.

HARD SAFETY (mirrors executor.is_demo): every entry point checks the connected
account is an IB PAPER account (id starts 'DU', and matches IB_ACCOUNT if set)
AND that we connected on a paper port. Refuses to act otherwise. Non-negotiable
and non-configurable -- a flag flip must never reach a live account.

What it does, per refresh cycle:
  - mirror_new():    for each newly OPEN paper trade of the live variant, resolve
                     the dated FRONT MONTH, SIZE BY SPECS (contracts.choose_contract,
                     micro fallback, skip-if-too-big), and place a bracket order
                     (parent market + attached stop + limit, OCA). Failed sends
                     (market closed, no contract) are retried next cycle.
  - sync_closures(): resolve paper trades from IB fills (broker truth). PLUS roll:
                     if an open position's contract enters its roll window, close
                     the front and re-open the next month carrying the trade.
  - reconcile():     join IB executions back onto the journal -> realized R vs
                     paper R.  CLI: uv run python -m dashboard.ib_exec
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401

import os
import math
import sqlite3
import datetime as dt

import pandas as pd

from dashboard.core import paper
from dashboard.core import store
from dashboard.data import ib_client
from dashboard.data import contracts
from dashboard.instruments import FUT_BY_KEY
from dashboard.core.log import log

MIRROR_METHOD = "ATR rr3.0"   # the one live variant we execute (same as MT5 exec)
SLEEVE_MAX_SPREAD_PCT = 0.005  # 0.5% of mid -- skip a sleeve entry if the live bid-ask spread
                               # is wider than this (VIX-panic spreads can blow out 5-10x vs a
                               # normal liquid ETF's ~0.01-0.05%; 0.5% is a generous cap, not a
                               # tight one). No historical intraday spread data exists to derive
                               # a rolling per-ticker baseline, so this is a fixed absolute cap.
DD_HALT_PCT = -13.0            # 2026-07-11: the ADOPTED PLAN's own text ("halt new entries if
                               # DD>-13%") was never actually wired up -- confirmed missing via
                               # a direct code search while fact-checking a critique. Existing
                               # positions are untouched; only NEW entries pause. Does not change
                               # backtest numbers (it's a live-only safety net for a black-swan
                               # scenario the backtest's fixed-parameter history can't rehearse),
                               # so there's nothing to sweep/optimize here -- it's a discipline
                               # gate, not an alpha lever. 0 disables (matches ETF_POS_CAP/
                               # PORTFOLIO_CAP's own convention).


# ---- pure sizing logic (no I/O -- unit-testable in isolation) ---------------

def cap_qty_to_portfolio_room(qty: int, price: float, equity_usd: float,
                              portfolio_cap: float, deployed_usd: float) -> int:
    """Scale a proposed share count DOWN (never up, never below 0) so this position's
    notional fits within whatever portfolio-level budget remains, given what's already
    deployed. Mirrors research.backtest's PORTFOLIO_CAP hybrid (see HANDOFF 2026-07-11):
    the per-position cap (ETF_POS_CAP) alone lets several concurrent positions each near
    25% stack past 100% total exposure -- this caps the AGGREGATE instead, without touching
    per-position sizing when there's room (0 disables, matching ETF_POS_CAP's convention)."""
    if portfolio_cap <= 0 or price <= 0:
        return qty
    room = max(equity_usd * portfolio_cap - deployed_usd, 0.0)
    return min(qty, int(room // price))


# ---- paper guard (non-negotiable) ------------------------------------------

def is_paper() -> bool:
    """True only when the connected IB account is a paper account."""
    return ib_client.is_paper()


def _guard():
    """Return the connected IB handle iff safe to trade, else None.

    DEFAULT (safe): paper accounts only (DU… id matching IB_ACCOUNT, reached via a paper
    port). Unchanged behaviour -- this is what the running system uses.

    LIVE opt-in (real money): ONLY when IB_ALLOW_LIVE=1 is EXPLICITLY set AND the connected
    account exactly equals IB_ACCOUNT AND we're on a live port. This lets a SEPARATE, isolated
    dashboard instance trade the live account while the paper instance keeps running untouched.
    The named-account match means a mis-set port/login can never trade the wrong account."""
    if not ib_client.is_available():
        return None
    port = int(os.environ.get("IB_PORT", "7497"))
    live_ok = os.environ.get("IB_ALLOW_LIVE", "").lower() in ("1", "true", "yes")
    if not live_ok:                                    # ---- paper-only (default) ----
        if not is_paper():
            log.warning("ib_exec: connected account is NOT paper -- refusing to trade")
            return None
        if port not in (7497, 4002):
            log.warning("ib_exec: IB_PORT %s is not a paper port -- refusing to trade", port)
            return None
    else:                                              # ---- explicit LIVE opt-in ----
        want = os.environ.get("IB_ACCOUNT")
        acct = ib_client.account_id()
        if not want or acct != want:
            log.warning("ib_exec: IB_ALLOW_LIVE set but account %s != IB_ACCOUNT %s -- refusing",
                        acct, want)
            return None
        if port not in (7496, 4001):
            log.warning("ib_exec: IB_ALLOW_LIVE set but IB_PORT %s is not a live port -- refusing",
                        port)
            return None
        log.warning("ib_exec: LIVE trading ENABLED for %s (IB_ALLOW_LIVE) -- REAL MONEY", acct)
    with ib_client._LOCK:
        return ib_client._ensure_conn()


# ---- mirror bookkeeping (same sqlite file as the journal) -------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(paper._DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS ib_mirror (
        paper_id INTEGER UNIQUE, perm_id INTEGER, con_id INTEGER,
        local_symbol TEXT, qty REAL, risk_money REAL, expiry TEXT,
        ts TEXT, status TEXT, note TEXT)""")
    return c


def _mirrored_ids() -> set[int]:
    with paper._LOCK, _conn() as c:
        return {r[0] for r in c.execute("SELECT paper_id FROM ib_mirror").fetchall()}


def mirrored_open_symbols() -> set[str]:
    """Symbols this dashboard believes are ACTUALLY OPEN at the broker (i.e. an order
    was placed and mirrored, and not yet marked closed) -- the correct comparison set
    for broker reconciliation. NOT the same as paper.all_trades() status=='OPEN', which
    tracks signal/idea state and can be OPEN for a trade that was never mirrored/filled."""
    with paper._LOCK, _conn() as c:
        return {r[0] for r in c.execute(
            "SELECT local_symbol FROM ib_mirror WHERE status='OPEN'").fetchall()}


# ---- actions ----------------------------------------------------------------

def mirror_new() -> list[str]:
    """Place paper bracket orders for OPEN paper trades of the live variant not
    yet mirrored. Returns human-readable log lines."""
    ib = _guard()
    if ib is None:
        return []
    dd_halt = float(os.environ.get("DD_HALT_PCT", str(DD_HALT_PCT)))
    if dd_halt < 0:
        hist, _ts = store.cache_get("equity_history")
        flows, _fts = store.cache_get("cash_flows")
        cur_dd = paper.current_drawdown_pct(hist or [], flows)
        if cur_dd <= dd_halt:
            msg = (f"DD-halt: current drawdown {cur_dd:.1f}% <= {dd_halt:.1f}% threshold -- "
                   "pausing ALL new entries this cycle (existing positions untouched)")
            log.warning("ib_exec: %s", msg)
            from dashboard.core import notable_events
            notable_events.record(msg, level="warning")
            return [msg]
    done = _mirrored_ids()
    logs: list[str] = []
    equity = _equity_usd(ib)                    # USD (US futures/ETFs price in USD)
    # PORTFOLIO_CAP (2026-07-11): ETF_POS_CAP alone only limits any ONE position -- with
    # several concurrent positions each near that cap, the total can still stack well past
    # 100% of equity (confirmed live: 7 positions summing to ~127%), which is what actually
    # drives the margin-debit interest cost, not the per-position cap itself. Backtested
    # hybrid (keep ETF_POS_CAP generous, cap the AGGREGATE too) strictly dominated every
    # pure-per-position-cap alternative tested (more CAGR AND better maxDD -- see HANDOFF).
    # `deployed` is a running total seeded from the broker's real GrossPositionValue and
    # incremented as each new position is placed THIS cycle, so multiple signals firing in
    # the same cycle can't collectively overshoot the cap (each only sees room actually left
    # after earlier ones in the same batch, same "walk the book chronologically" logic the
    # backtest itself uses).
    # FIXED 2026-07-13: GrossPositionValue alone only counts FILLED positions -- a pending,
    # not-yet-filled order (e.g. placed outside market hours) contributed nothing here,
    # letting PORTFOLIO_CAP be silently breached (confirmed live: 6 pending orders already
    # totalled ~125% of equity before IBKR's OWN margin check, not this cap, cancelled the
    # 7th). _pending_entry_notional_usd() adds that missing commitment.
    deployed = [_gpv_usd(ib) + _pending_entry_notional_usd()]
    # Error 435 "You must specify an account" (confirmed live 2026-07-10): IBKR requires an
    # explicit order.account whenever the login manages MORE than one account (this one does:
    # the real U12991898 + an unrelated empty U20738951) -- without it, orders get silently
    # cancelled. Fetched ONCE here (outside any call()/_run() closure -- account_id() does its
    # own loop-thread round-trip, so calling it FROM inside one would self-deadlock) and passed
    # down to every order-placing helper below.
    acct = ib_client.account_id()
    # PHASE auto-switch: Phase 1 = core only; Phase 2 (equity >= PHASE2_NAV_USD) also runs the
    # panic-MR sleeve, IF ALSO explicitly enabled (sleeve.sleeve_enabled(), paper-only by
    # default -- see sleeve.py). Both gates independent so the sleeve never silently activates.
    from dashboard.instruments import ETF_TRADED_BY_KEY
    from dashboard.core import sleeve
    for t in paper.open_trades():
        if t["id"] in done:
            continue
        if t["method"] == sleeve.SLEEVE_METHOD:
            if t["instrument"] not in sleeve.SLEEVE_UNIVERSE:
                continue
            msg = _place_sleeve_bracket(ib, t, equity, acct, deployed)  # sleeve (see sleeve.SLEEVE_UNIVERSE)
        elif t["method"] == MIRROR_METHOD:
            spec = contracts.SPECS.get(t["instrument"])
            if spec is not None:
                msg = _place_bracket(ib, t, spec, equity, acct)         # futures (no portfolio-cap yet)
            elif t["instrument"] in ETF_TRADED_BY_KEY:
                msg = _place_etf_bracket(ib, t, equity, acct, deployed)  # ETF (shares)
            else:
                continue
        else:
            continue
        if msg:
            logs.append(msg); log.info("ib_exec: %s", msg)
    return logs


def _equity_usd(ib) -> float:
    """Net liquidation value in USD. The paper account base may be non-USD (HKD here);
    US futures/ETFs price in USD, so we convert -- WITHOUT this, sizing compared an HKD
    budget to USD risk and would oversize ~8x. Falls back to the configured notional."""
    summ = ib_client.account_summary()
    if summ and summ.get("NetLiquidation") is not None:
        nl = summ["NetLiquidation"]
        rate = ib_client.fx_to_usd(summ.get("_ccy", "USD"))
        if rate:
            return nl * rate
    return paper.ACCOUNT


def _gpv_usd(ib) -> float:
    """Current GrossPositionValue in USD -- the broker's own real-time mark of everything
    already deployed (not a local reconstruction from entry prices, which can drift from
    reality -- see the 2026-07-10 ghost-position incident). 0.0 if unavailable (fails open:
    a missing reading means the portfolio-cap check below just sees no deployed exposure
    yet, same conservative direction as _equity_usd falling back to paper.ACCOUNT)."""
    summ = ib_client.account_summary()
    if summ and summ.get("GrossPositionValue") is not None:
        rate = ib_client.fx_to_usd(summ.get("_ccy", "USD"))
        if rate:
            return summ["GrossPositionValue"] * rate
    return 0.0


def _pending_entry_notional_usd() -> float:
    """Sum of entry notional (qty x entry price, USD -- US ETFs price in USD, no FX needed)
    for every symbol with a REAL order sitting at the broker that hasn't filled yet.

    FOUND 2026-07-13: `_gpv_usd()` (GrossPositionValue) only reflects FILLED positions --
    while an order sits pending (e.g. placed outside market hours, exactly what happened:
    6 orders placed ~9.5h before the US market opened), it contributes ZERO to `deployed`,
    so PORTFOLIO_CAP sizing sees "no exposure yet" and happily sizes the NEXT signal as if
    there's full room. Confirmed live: 6 pending orders alone already totalled ~125% of
    equity -- PORTFOLIO_CAP=1.0 was silently breached before a 7th order (CWB) got cancelled
    by IBKR's OWN margin check, not by this bot's safety mechanism. This closes that gap by
    adding pending (not-yet-filled) commitment to the same `deployed` figure PORTFOLIO_CAP
    already uses, using LOCAL records (ib_mirror qty x paper_trades entry) for the notional
    -- no extra broker price lookups needed, and a symbol here is guaranteed not-yet-filled
    (broker_open_order_symbols() only returns orders IBKR hasn't resolved to a terminal
    state) so this can never double-count against `_gpv_usd()`."""
    pending_syms = ib_client.broker_open_order_symbols()
    if not pending_syms:
        return 0.0
    # exclude anything the broker ALREADY shows as a filled position (e.g. only the SL/TP
    # children remain open after the parent filled) -- that's already inside GrossPositionValue.
    broker_pos = ib_client.broker_positions() or {}
    pending_syms = pending_syms - set(broker_pos.keys())
    if not pending_syms:
        return 0.0
    total = 0.0
    with paper._LOCK, _conn() as c:
        for sym in pending_syms:
            row = c.execute(
                "SELECT m.qty, p.entry FROM ib_mirror m JOIN paper_trades p "
                "ON m.paper_id = p.id WHERE m.local_symbol=? AND m.status='OPEN'",
                (sym,)).fetchone()
            if row:
                total += float(row[0]) * float(row[1])
    return total


def current_equity_usd() -> float | None:
    """PUBLIC equity accessor for callers outside this module (e.g. sleeve signal
    generation, which needs equity for phase-gating but must stay broker-agnostic in
    dashboard.core). Paper-guarded like everything else here; None if not connected."""
    ib = _guard()
    if ib is None:
        return None
    return _equity_usd(ib)


def current_portfolio_room_usd() -> float | None:
    """PUBLIC: USD notional still available under PORTFOLIO_CAP right now (filled +
    pending commitment subtracted from equity x cap) -- None if not connected, or if
    PORTFOLIO_CAP is disabled (0, meaning "no cap" -- there's no meaningful "room" to report).
    Added 2026-07-13 alongside app.py's _pending_reason() fix: a signal correctly held back
    by cap_qty_to_portfolio_room() (e.g. SPY/QQQ/IWM, confirmed live) used to show the same
    "awaiting the next mirror cycle" message as a signal about to place normally -- misleading,
    since it will NEVER place until room frees up. This lets the UI tell them apart."""
    ib = _guard()
    if ib is None:
        return None
    portfolio_cap = float(os.environ.get("PORTFOLIO_CAP", "1.0"))
    if portfolio_cap <= 0:
        return None
    equity = _equity_usd(ib)
    deployed = _gpv_usd(ib) + _pending_entry_notional_usd()
    return max(equity * portfolio_cap - deployed, 0.0)


def _place_bracket(ib, t: dict, spec: contracts.FutureSpec, equity: float,
                   acct: str | None = None) -> str | None:
    inst = FUT_BY_KEY.get(t["instrument"])
    contract = ib_client.front_future(spec, dt.date.today())
    if contract is None:
        return f"{t['instrument']}: no front contract (market data?), retry"
    stop_points = abs(float(t["entry"]) - float(t["sl"]))
    chosen, qty = contracts.choose_contract(spec, equity, stop_points,
                                            paper.RISK_PER_TRADE)
    if qty < 1:
        return f"{t['instrument']}: 1 contract risks > budget even as micro, SKIP"
    # if sizing picked the micro sibling, re-resolve the contract for THAT symbol
    if chosen.key != spec.key:
        contract = ib_client.front_future(chosen, dt.date.today())
        if contract is None:
            return f"{t['instrument']}: no front for micro {chosen.symbol}, retry"
    ib_async = ib_client._mod()
    action = "BUY" if t["direction"] == "long" else "SELL"
    risk_money = equity * paper.RISK_PER_TRADE
    def send():
        bracket = ib.bracketOrder(action, qty, limitPrice=0.0,
                                  takeProfitPrice=float(t["tp"]),
                                  stopLossPrice=float(t["sl"]))
        # parent as MARKET (bracketOrder makes a LMT parent by default)
        bracket.parent.orderType = "MKT"
        bracket.parent.lmtPrice = 0.0
        for o in bracket:
            o.tif = "GTC"
            o.orderRef = f"quant#{t['id']}"
            if acct:
                o.account = acct
        return [ib.placeOrder(contract, o) for o in bracket]   # non-blocking; loop transmits
    try:
        trades = ib_client.call(send, timeout=15)
    except Exception as e:                     # noqa: BLE001
        return f"{t['instrument']}: order send failed ({e}), retry"
    parent = trades[0].order
    perm_id = getattr(parent, "permId", 0)
    with paper._LOCK, _conn() as c:
        c.execute("INSERT OR IGNORE INTO ib_mirror VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (t["id"], perm_id, getattr(contract, "conId", 0),
                   getattr(contract, "localSymbol", chosen.symbol), qty, risk_money,
                   getattr(contract, "lastTradeDateOrContractMonth", ""),
                   dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "OPEN", ""))
    return (f"{t['instrument']}: paper bracket placed {action} {qty}x "
            f"{getattr(contract, 'localSymbol', chosen.symbol)} "
            f"SL {t['sl']} TP {t['tp']}")


def _place_etf_bracket(ib, t: dict, equity_usd: float, acct: str | None = None,
                       deployed: list[float] | None = None) -> str | None:
    """ETF order: SHARE-based sizing (shares = floor(risk_$ / stop_per_share)) and a
    SMART stock bracket. No contract specs/rolls -- ETFs are simpler than futures and
    divide finely, so any account size works."""
    contract = ib_client.stock_contract(t["instrument"])      # symbol == key (GLD, SPY…)
    if contract is None:
        return f"{t['instrument']}: no stock contract (market data?), retry"
    stop_per_share = abs(float(t["entry"]) - float(t["sl"]))
    qty = contracts.size_shares(equity_usd, stop_per_share, paper.RISK_PER_TRADE)
    # Per-position NOTIONAL cap: risk-based sizing on a low-vol ETF (e.g. SHY) buys a huge
    # share count to risk 0.5%, which on a small account over-levers (SHY alone > 100% of a
    # $12.8k acct). Cap notional to a fraction of equity. Backtest (research.backtest --pos-cap):
    # 20% cap costs ~-1pp CAGR but cuts maxDD -9.4%->-6.3% and lifts Sharpe 1.19->1.29. Env
    # ETF_POS_CAP overrides (0 disables). Matches the backtest's cap+risk-scaling.
    # The cap is the real return/DD dial (strategy-only Sharpe is flat ~0.88 at every cap; the
    # higher blended Sharpe at tight caps is just idle-cash yield). ADOPTED 0.25 @ 1% risk =
    # ~7.5% CAGR / -8.8% DD blended (fills the cap on high-vol names, mild safe leverage).
    pos_cap = float(os.environ.get("ETF_POS_CAP", "0.25"))
    price = float(t["entry"])
    if pos_cap > 0 and price > 0:
        qty = min(qty, int(math.floor(equity_usd * pos_cap / price)))
    # PORTFOLIO_CAP (2026-07-11): the per-position cap alone lets several concurrent positions
    # each near 25% stack past 100% total exposure (confirmed live: 7 positions -> ~127%,
    # driving the margin-debit interest cost). Backtested hybrid (keep ETF_POS_CAP generous,
    # cap the AGGREGATE too via research.backtest's PORTFOLIO_CAP) strictly beat every pure
    # per-position-cap alternative -- more CAGR AND better maxDD (see HANDOFF). Scales this
    # entry DOWN to whatever room is left (never skips outright), same "scale don't skip"
    # philosophy as the per-position cap above. 0 disables (matches ETF_POS_CAP's convention).
    portfolio_cap = float(os.environ.get("PORTFOLIO_CAP", "1.0"))
    if deployed is not None:
        qty = cap_qty_to_portfolio_room(qty, price, equity_usd, portfolio_cap, deployed[0])
    if qty < 1:
        return f"{t['instrument']}: <1 share at the risk/cap budget, SKIP"
    action = "BUY" if t["direction"] == "long" else "SELL"
    risk_money = equity_usd * paper.RISK_PER_TRADE
    if deployed is not None:
        deployed[0] += qty * price          # so later signals THIS cycle see the updated total
    # US ETFs trade on a $0.01 tick; IB rejects (Error 110) child legs whose price
    # carries sub-penny precision, leaving the position UNPROTECTED. Round to cents.
    tp_px = round(float(t["tp"]), 2)
    sl_px = round(float(t["sl"]), 2)

    def send():
        bracket = ib.bracketOrder(action, qty, limitPrice=0.0,
                                  takeProfitPrice=tp_px,
                                  stopLossPrice=sl_px)
        bracket.parent.orderType = "MKT"
        bracket.parent.lmtPrice = 0.0
        for o in bracket:
            o.tif = "GTC"
            o.orderRef = f"quant#{t['id']}"
            if acct:
                o.account = acct
        return [ib.placeOrder(contract, o) for o in bracket]
    try:
        trades = ib_client.call(send, timeout=15)
    except Exception as e:                     # noqa: BLE001
        return f"{t['instrument']}: order send failed ({e}), retry"
    perm_id = getattr(trades[0].order, "permId", 0)
    with paper._LOCK, _conn() as c:
        c.execute("INSERT OR IGNORE INTO ib_mirror VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (t["id"], perm_id, getattr(contract, "conId", 0), t["instrument"],
                   qty, risk_money, "",
                   dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "OPEN", "etf"))
    msg = (f"{t['instrument']}: paper bracket placed {action} {qty}sh "
          f"SL {sl_px} TP {tp_px}")
    from dashboard.core import notable_events
    notable_events.record(f"New order placed: {msg}")
    return msg


def _place_sleeve_bracket(ib, t: dict, equity_usd: float, acct: str | None = None,
                          deployed: list[float] | None = None) -> str | None:
    """Panic-MR sleeve order: SAME bracket mechanics as _place_etf_bracket (a real broker
    STP -5% / LMT +3% pair protects the position even if this app is offline), but sized at
    the SLEEVE's own risk_pct (0.5% base / 1.0% at VIX>30), read from entry_facts -- NOT the
    core's global paper.RISK_PER_TRADE. The dynamic 5MA-touch/10-day exits are separate
    (sleeve.close_expired_sleeves), since a static broker order can't express them."""
    import json
    try:
        risk_pct = json.loads(t.get("entry_facts") or "{}").get("risk_pct")
    except Exception:                                   # noqa: BLE001
        risk_pct = None
    if risk_pct is None:
        return f"{t['instrument']}: sleeve trade missing risk_pct in entry_facts, SKIP"
    contract = ib_client.stock_contract(t["instrument"])
    if contract is None:
        return f"{t['instrument']}: no stock contract (market data?), retry"
    stop_per_share = abs(float(t["entry"]) - float(t["sl"]))
    qty = contracts.size_shares(equity_usd, stop_per_share, risk_pct)
    pos_cap = float(os.environ.get("ETF_POS_CAP", "0.25"))   # same safety cap as the core
    price = float(t["entry"])
    if pos_cap > 0 and price > 0:
        qty = min(qty, int(math.floor(equity_usd * pos_cap / price)))
    # PORTFOLIO_CAP: same aggregate-exposure guard as _place_etf_bracket -- see its comment.
    portfolio_cap = float(os.environ.get("PORTFOLIO_CAP", "1.0"))
    if deployed is not None:
        qty = cap_qty_to_portfolio_room(qty, price, equity_usd, portfolio_cap, deployed[0])
    if qty < 1:
        return f"{t['instrument']}: <1 share at the risk/cap budget, SKIP"
    # SPREAD-WIDENING GUARD (2026-07-09): the sleeve's whole thesis is entering during a VIX
    # panic, exactly when ETF bid-ask spreads can blow out 5-10x -- filling a bracket's MARKET
    # parent order into a wide spread means paying the worst possible price right when the
    # signal fires. Backtests assume close-price fills (no spread cost modeled here at all),
    # so this is a real, previously-uncovered execution risk, not just a refinement. Skip (not
    # cancel -- the trade row stays OPEN/unmirrored so the next mirror_new() cycle retries) if
    # the live spread is wider than SLEEVE_MAX_SPREAD_PCT of mid price. Logged either way for
    # audit ("skipped due to spread" is itself useful information, not just silence).
    tick = ib_client.get_stock_tick(t["instrument"])
    if tick and tick.get("mid"):
        spread_pct = tick["spread"] / tick["mid"] if tick["mid"] else 0.0
        if spread_pct > SLEEVE_MAX_SPREAD_PCT:
            log.warning("ib_exec: sleeve #%s %s SKIPPED, spread %.2f%% > cap %.2f%% "
                        "(bid %.2f / ask %.2f)", t.get("id"), t["instrument"],
                        spread_pct * 100, SLEEVE_MAX_SPREAD_PCT * 100,
                        tick["bid"], tick["ask"])
            return (f"{t['instrument']}: spread {spread_pct:.2%} > cap "
                    f"{SLEEVE_MAX_SPREAD_PCT:.2%} (bid {tick['bid']:.2f}/ask {tick['ask']:.2f}), "
                    "SKIP for now, retry next cycle")
    # tick is None (no real-time market-data subscription, or a transient fetch failure) ->
    # deliberately fall through and place the order anyway, matching how the rest of this
    # codebase treats missing live quotes elsewhere -- a permanent block on every missing-quote
    # cycle would silently starve the sleeve of all its trades on a delayed-data account.
    if deployed is not None:                # only reserve budget once past every skip check
        deployed[0] += qty * price
    action = "BUY"                                       # sleeve is long-only
    risk_money = equity_usd * risk_pct
    tp_px = round(float(t["tp"]), 2)
    sl_px = round(float(t["sl"]), 2)

    def send():
        bracket = ib.bracketOrder(action, qty, limitPrice=0.0,
                                  takeProfitPrice=tp_px, stopLossPrice=sl_px)
        bracket.parent.orderType = "MKT"
        bracket.parent.lmtPrice = 0.0
        for o in bracket:
            o.tif = "GTC"
            o.orderRef = f"sleeve#{t['id']}"
            if acct:
                o.account = acct
        return [ib.placeOrder(contract, o) for o in bracket]
    try:
        trades = ib_client.call(send, timeout=15)
    except Exception as e:                     # noqa: BLE001
        return f"{t['instrument']}: sleeve order send failed ({e}), retry"
    perm_id = getattr(trades[0].order, "permId", 0)
    with paper._LOCK, _conn() as c:
        c.execute("INSERT OR IGNORE INTO ib_mirror VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (t["id"], perm_id, getattr(contract, "conId", 0), t["instrument"],
                   qty, risk_money, "",
                   dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "OPEN", "sleeve"))
    msg = (f"{t['instrument']}: SLEEVE bracket placed {action} {qty}sh "
          f"SL {sl_px} TP {tp_px} (risk {risk_pct:.1%})")
    from dashboard.core import notable_events
    notable_events.record(f"New SLEEVE order placed: {msg}")
    return msg


def manual_close_sleeve(trade: dict, reason: str) -> str | None:
    """Flatten a sleeve position for a DYNAMIC exit (5MA-touch / time-cap) that a static
    broker bracket can't express: cancel the outstanding STP+LMT children, then submit a
    market SELL to flatten. Does NOT resolve the paper trade itself -- the next
    sync_closures() cycle picks up the resulting broker-truth closing fill exactly like any
    other exit (that code path is method-agnostic, already reused as-is)."""
    ib = _guard()
    if ib is None:
        return None
    acct = ib_client.account_id()          # see mirror_new()'s comment re: Error 435
    with paper._LOCK, _conn() as c:
        row = c.execute("SELECT con_id, qty, status FROM ib_mirror WHERE paper_id=?",
                        (trade["id"],)).fetchone()
    if row is None or row[2] != "OPEN":
        return None
    con_id, qty, _ = row

    def _do():
        contract = next((p.contract for p in (ib.positions() or [])
                         if p.contract.conId == con_id), None)
        if contract is None:
            return None                                  # already flat; sync_closures handles it
        # ib.reqAllOpenOrders() is a SYNC wrapper that internally calls
        # util.run() -> loop.run_until_complete() -- illegal from inside a callback
        # already executing ON that same running loop (which is exactly where _do()
        # runs, via ib_client.call() below), and raises "This event loop is already
        # running" (confirmed live: 422+ occurrences in logs since 2026-06-25, several
        # of which cascaded into sync_closures() failures on the next cycle). Use the
        # passive in-memory cache (ib.openTrades(), same shape as reqAllOpenOrders()'s
        # result) instead -- no I/O, just reads what the account-sync subscription has
        # already delivered, same pattern as ib.positions()/ib.fills() elsewhere here.
        for o in (ib.openTrades() or []):
            if o.contract.conId == con_id:
                ib.cancelOrder(o.order)
        import ib_async
        market = ib_async.MarketOrder("SELL", abs(qty))
        market.orderRef = f"sleeve-exit#{trade['id']}"
        if acct:
            market.account = acct
        return ib.placeOrder(contract, market)
    try:
        sent = ib_client.call(_do, timeout=15)
    except Exception as e:                     # noqa: BLE001
        return f"{trade['instrument']}: sleeve exit send failed ({e}), retry"
    if sent is None:
        return None
    return f"{trade['instrument']}: sleeve DYNAMIC EXIT ({reason}) -- flatten order sent"


def sync_closures() -> list[str]:
    """Resolve paper trades from IB fills (broker truth) and roll positions whose
    contract is entering its expiry window."""
    ib = _guard()
    if ib is None:
        return []
    acct = ib_client.account_id()          # see mirror_new()'s comment re: Error 435
    logs: list[str] = []
    journal = {t["id"]: t for t in paper.all_trades()}
    with paper._LOCK, _conn() as c:
        rows = c.execute("SELECT paper_id, con_id, local_symbol, qty, expiry, "
                         "status FROM ib_mirror").fetchall()
    # async order request runs on the loop thread via _run (the sync reqAllOpenOrders
    # wrapper would try to start the already-running loop -> RuntimeError). Wrap in a
    # coroutine since the *Async method returns a Future, not a bare coroutine.
    async def _fetch_orders():
        return await ib.reqAllOpenOrdersAsync()
    open_trades = ib_client._run(_fetch_orders()) or []
    working_conids = {t.contract.conId for t in open_trades
                      if t.orderStatus.status in
                      ("ApiPending", "PendingSubmit", "PreSubmitted", "Submitted")}
    positions = ib_client.call(lambda: {p.contract.conId: p for p in (ib.positions() or [])})
    for paper_id, con_id, local_symbol, qty, expiry, mstatus in rows:
        pt = journal.get(paper_id)
        if pt is None or mstatus == "CLOSED":
            continue
        open_pos = positions.get(con_id)
        # GUARD: "no position" can mean NOT-YET-FILLED (parent order still working,
        # e.g. placed while the market was closed), NOT just exited. Only treat it as
        # closed when there is ALSO no working order for this contract -- otherwise the
        # trade is pending and would be wrongly closed before it ever fills.
        if (open_pos is None or open_pos.position == 0) and con_id in working_conids:
            # FIXED 2026-07-13: paper.resolve_open() runs independently on every tick cycle
            # (broker-agnostic, checks REAL market OHLC/ticks against the trade's stored
            # SL/TP/HORIZON_CAL=35d regardless of whether a real broker fill ever happened --
            # see service.py's call site) and can resolve a trade to WIN/LOSS/EXPIRED while
            # its real bracket order is STILL working/unfilled at the broker (e.g. price
            # gapped past the stored levels before the order ever filled, or the 35-day
            # horizon simply ran out). Before this fix, that case just fell through this
            # `continue` forever -- the real order would sit at the broker indefinitely,
            # orphaned, with nothing ever cancelling it, since paper already considered the
            # trade done and would never ask again. Cancel the stale working order(s) for
            # this contract (parent + any still-working children -- OCA groups do NOT
            # auto-cancel siblings on a manual cancel, only on a FILL) and mark the mirror
            # row closed to match, instead of leaving a real order live with no local trade
            # tracking it anymore.
            if pt["status"] != "OPEN":
                for o in open_trades:
                    if o.contract.conId == con_id:
                        ib.cancelOrder(o.order)
                with paper._LOCK, _conn() as c:
                    c.execute("UPDATE ib_mirror SET status='CLOSED', note=? WHERE paper_id=?",
                              (f"order cancelled: paper independently resolved to "
                               f"{pt['status']} while still unfilled at the broker", paper_id))
                msg = (f"{local_symbol}: cancelled stale unfilled order (paper already "
                      f"resolved {pt['status']} via real price/horizon, not a broker fill)")
                logs.append(msg); log.info("ib_exec: %s", msg)
                from dashboard.core import notable_events
                notable_events.record(msg, level="warning")
            continue
        # (a) position closed at broker (SL/TP filled) while paper still OPEN ->
        #     resolve the paper trade from the broker's actual exit.
        # BUG FIXED 2026-07-02: this used to mark ib_mirror CLOSED unconditionally, even when
        # _resolve_from_broker() found no matching closing fill (returns None) -- e.g. a
        # TRANSIENT/incomplete ib.positions() read right after a reconnect can look identical
        # to "genuinely flat". That permanently orphaned the mirror row as CLOSED while the
        # paper journal correctly stayed OPEN, so live_positions() (WHERE status='OPEN') would
        # silently drop a still-open position forever -- broke the portfolio pie/allocation.
        # Fix: only commit CLOSED once we've actually confirmed it (a real closing fill found,
        # OR the paper side already resolved some other way) -- same "uncertain -> retry next
        # cycle, don't commit" philosophy as the working-order guard above.
        if open_pos is None or open_pos.position == 0:
            if pt["status"] == "OPEN":
                msg = _resolve_from_broker(ib, pt, con_id)
                if msg:
                    with paper._LOCK, _conn() as c:
                        c.execute("UPDATE ib_mirror SET status='CLOSED' WHERE paper_id=?",
                                  (paper_id,))
                    logs.append(msg); log.info("ib_exec: %s", msg)
                # else: no confirming fill yet -- leave ib_mirror OPEN, re-check next cycle.
            else:
                # paper already resolved (e.g. via the deterministic tick path); nothing left
                # to resolve, safe to mark the mirror row closed now.
                with paper._LOCK, _conn() as c:
                    c.execute("UPDATE ib_mirror SET status='CLOSED' WHERE paper_id=?",
                              (paper_id,))
            continue
        # (b) roll: open position inside its contract's roll window -> close front,
        #     re-open next month carrying the same paper trade.
        spec = contracts.SPECS.get(pt["instrument"])
        exp = _parse_expiry(expiry)
        if spec is not None and exp is not None and contracts.needs_roll(exp, spec):
            msg = _roll_position(ib, pt, spec, con_id, qty, acct)
            if msg:
                logs.append(msg); log.info("ib_exec: %s", msg)
    return logs


def _resolve_from_broker(ib, trade: dict, con_id: int) -> str | None:
    """Resolve a paper trade from its mirrored position's actual fills."""
    exit_price = _last_exit_price(ib, con_id)
    if exit_price is None:
        return None
    # futures cost: real commission + tick slippage (price points), not the CFD
    # half-spread fraction.
    spec = contracts.SPECS.get(trade["instrument"])
    cost_abs = contracts.cost_points(spec) if spec else None
    r = paper.r_multiple(trade["direction"], trade["entry"], trade["sl"], exit_price,
                         half_spread=trade.get("half_spread") or paper.HALF_SPREAD,
                         cost_abs=cost_abs)
    status = "WIN" if r > 0 else "LOSS"
    exit_ts = pd.Timestamp.now(tz="UTC")
    paper._update_resolution(trade["id"], status, str(exit_ts), exit_price,
                             round(r, 3), exit_reason="closed at broker (IB)")
    msg = (f"#{trade['id']} {trade['instrument']} resolved from BROKER (IB): "
          f"{status} R={r:+.2f} exit={exit_price}")
    from dashboard.core import notable_events
    notable_events.record(f"Position closed: {msg}")
    return msg


def _last_exit_price(ib, con_id: int) -> float | None:
    """Average price of the most recent closing fill for con_id, or None."""
    fills = ib_client.call(lambda: [f for f in (ib.fills() or [])
                                    if getattr(f.contract, "conId", None) == con_id])
    if not fills:
        return None
    return float(fills[-1].execution.avgPrice or fills[-1].execution.price)


def _roll_position(ib, trade: dict, spec: contracts.FutureSpec, old_con_id: int,
                   qty: float, acct: str | None = None) -> str | None:
    """Close the expiring front contract and re-open the next month at market,
    keeping the same SL/TP and paper-trade id. Updates the mirror row."""
    new_contract = ib_client.front_future(spec, dt.date.today() +
                                          dt.timedelta(days=spec.roll_offset_days + 7))
    if new_contract is None or getattr(new_contract, "conId", 0) == old_con_id:
        return None  # no next month resolvable yet; try again next cycle
    ib_async = ib_client._mod()
    close_act = "SELL" if trade["direction"] == "long" else "BUY"
    open_act = "BUY" if trade["direction"] == "long" else "SELL"
    def do_roll():
        old = next((p.contract for p in (ib.positions() or [])
                    if p.contract.conId == old_con_id), None)
        if old is not None:
            close_o = ib_async.MarketOrder(close_act, qty)
            if acct:
                close_o.account = acct
            ib.placeOrder(old, close_o)
        parent = ib_async.MarketOrder(open_act, qty)
        sl = ib_async.StopOrder(close_act, qty, float(trade["sl"]))
        tp = ib_async.LimitOrder(close_act, qty, float(trade["tp"]))
        for o in (parent, sl, tp):
            o.orderRef = f"quant#{trade['id']}"; o.tif = "GTC"
            if acct:
                o.account = acct
        ib.placeOrder(new_contract, parent)
        ib.placeOrder(new_contract, sl)
        ib.placeOrder(new_contract, tp)
    try:
        ib_client.call(do_roll, timeout=15)
    except Exception as e:                     # noqa: BLE001
        return f"#{trade['id']} {trade['instrument']}: ROLL FAILED ({e}), retry"
    with paper._LOCK, _conn() as c:
        c.execute("UPDATE ib_mirror SET con_id=?, local_symbol=?, expiry=?, "
                  "note='rolled' WHERE paper_id=?",
                  (getattr(new_contract, "conId", 0),
                   getattr(new_contract, "localSymbol", spec.symbol),
                   getattr(new_contract, "lastTradeDateOrContractMonth", ""),
                   trade["id"]))
    return (f"#{trade['id']} {trade['instrument']}: ROLLED to "
            f"{getattr(new_contract, 'localSymbol', spec.symbol)}")


# ---- read-only views (UI / analysis) ---------------------------------------

def live_positions() -> dict | None:
    """Map paper_id -> live IB position for OUR trades (matched via the ib_mirror
    table's con_id). Returns None on a CONNECTION failure (so callers keep last-good)
    vs {} when genuinely flat. Mirrors executor.live_positions output shape."""
    if not ib_client.is_available():
        return None
    ib = ib_client._ensure_conn()
    if ib is None:
        return None
    with paper._LOCK, _conn() as c:
        rows = c.execute("SELECT paper_id, con_id, qty FROM ib_mirror "
                         "WHERE status='OPEN'").fetchall()
    try:
        positions, portfolio = ib_client.call(lambda: (
            {p.contract.conId: p for p in (ib.positions() or [])},
            {i.contract.conId: i for i in (ib.portfolio() or [])}))
    except Exception:                                  # noqa: BLE001 -- read failed, keep last-good
        return None
    out: dict[int, dict] = {}
    for paper_id, con_id, qty in rows:
        p = positions.get(con_id)
        if p is None or p.position == 0:
            continue
        pf = portfolio.get(con_id)
        out[paper_id] = {
            "ticket": con_id, "open": float(p.avgCost or 0),
            "profit": float(pf.unrealizedPNL) if pf else 0.0,
            "volume": float(abs(p.position)),
            "direction": "long" if p.position > 0 else "short"}
    return out


# --- keep cash in USD (clear the USD margin debit; earn USD interest) -------------
def _ledger_cash(ib) -> dict:
    """{currency: CashBalance} from the account ledger (per-currency, not base-consolidated)."""
    async def _fetch():
        return await ib.accountSummaryAsync()
    summ = ib_client._run(_fetch())
    out = {}
    for v in summ:
        if v.tag == "$LEDGER-CashBalance" and v.currency in ("USD", "HKD"):
            try:
                out[v.currency] = float(v.value)
            except (TypeError, ValueError):
                pass
        if v.tag == "$LEDGER-ExchangeRate" and v.currency == "USD":
            try:                                       # USD ledger ExchangeRate = HKD per 1 USD
                out["_hkd_per_usd"] = float(v.value)
            except (TypeError, ValueError):
                pass
    return out


def keep_cash_usd() -> dict:
    """Convert idle HKD cash to USD so it earns USD interest (and CLEARS any USD margin
    debit, which costs ~5-6%). Opt-in via CASH_USD=1, paper-guarded. Converts HKD down to
    a small residual buffer. Returns status for the dashboard."""
    status = {"enabled": os.environ.get("CASH_USD", "").lower() in ("1", "true", "yes"),
              "ok": False, "usd_cash": 0.0, "hkd_cash": 0.0, "log": "", "stuck": False}
    if not status["enabled"]:
        return status
    ib = _guard()
    if ib is None:
        return status
    acct = ib_client.account_id()          # see mirror_new()'s comment re: Error 435
    led = _ledger_cash(ib)
    if "USD" not in led and "HKD" not in led:            # ledger read failed -> keep last-good
        return status
    status["ok"] = True
    hkd = led.get("HKD", 0.0)
    status["usd_cash"] = led.get("USD", 0.0)
    status["hkd_cash"] = hkd
    from dashboard.core import store
    if status["usd_cash"] > 1:            # a genuine past fill -- clear the stuck-attempt counter
        store.cache_set("keep_cash_usd_attempts", 0)
    rate = led.get("_hkd_per_usd", 7.84)              # HKD per USD
    KEEP_HKD = 500.0                                  # leave a tiny HKD residual
    if hkd <= KEEP_HKD or rate <= 0:
        return status
    usd_to_buy = int((hkd - KEEP_HKD) / rate)         # BUY USD, SELL HKD
    if usd_to_buy < 1:
        return status
    if os.environ.get("CASH_USD_DRYRUN", "").lower() in ("1", "true", "yes"):
        status["log"] = (f"keep-cash-usd DRYRUN: would convert HKD {hkd:,.0f} -> "
                         f"BUY ${usd_to_buy:,} (USD cash now {status['usd_cash']:,.0f})")
        log.info("ib_exec: %s", status["log"])
        return status
    # RETRY COOLDOWN (2026-07-08): placeOrder() is fire-and-forget -- a rejection comes back
    # async via an error event we don't listen for, never as an exception here, so a failing
    # order (e.g. Forex trading not yet approved on the account) looked identical to success
    # and got resubmitted every single refresh cycle (~70-90s) indefinitely -- 224+ live order
    # attempts over 3.5h with zero actual fills before this was caught. Only retry every 5min
    # (tightened from 20min 2026-07-09, once the account's Leveraged Forex permission gap was
    # identified -- 5min gives a faster confirmation once that's enabled, while still well
    # above the ~70-90s refresh cadence so a genuinely-broken case doesn't spam the API).
    # STUCK TRACKING: a persistent attempts counter survives across cycles/restarts (unlike a
    # local variable) so the dashboard can show a warning badge once repeated attempts have
    # produced no real USD balance -- surfaced via status["stuck"] regardless of which branch
    # below returns (cooldown-skip included), so the badge doesn't flicker off between retries.
    import time as _time
    attempts, _ats = store.cache_get("keep_cash_usd_attempts")
    attempts = attempts or 0
    status["stuck"] = attempts >= 2
    last, _lts = store.cache_get("keep_cash_usd_last_attempt")
    now_s = int(_time.time())
    if last and now_s - last < 300:
        status["log"] = "keep-cash-usd: cooling down after a recent attempt (retries every 5min)"
        return status
    store.cache_set("keep_cash_usd_last_attempt", now_s)
    store.cache_set("keep_cash_usd_attempts", attempts + 1)
    import ib_async
    fx = ib_async.Forex("USDHKD")

    async def _qualify():                              # qualify on the loop thread (no nesting)
        await ib.qualifyContractsAsync(fx)
        return fx
    try:
        ib_client._run(_qualify())
        o = ib_async.MarketOrder("BUY", usd_to_buy)   # BUY USD base, pay HKD
        o.orderRef = "keep-cash-usd"
        # Without an explicit TIF, IBKR defaults this to DAY "based on order preset" and then
        # cancels it outright (Error 10349) -- confirmed live 2026-07-10, a SEPARATE bug from
        # the missing-account one above (both silently blocked every past attempt). GTC (same
        # as every other order type in this file) reaches Submitted; small size just gets a
        # benign Warning 399 (below IdealPro's $25k minimum, routed as an odd lot).
        o.tif = "GTC"
        if acct:
            o.account = acct
        trade = ib_client.call(lambda: ib.placeOrder(fx, o))
    except Exception as e:                             # noqa: BLE001
        status["log"] = f"keep-cash-usd: FX order failed ({e})"
        log.warning("ib_exec: %s", status["log"])
        return status
    # best-effort immediate status check -- placeOrder() doesn't block for a fill, so this
    # only catches a FAST rejection, not a delayed one; the 5min cooldown is the real safety net
    st = getattr(trade.orderStatus, "status", "") if trade else ""
    if st in ("Cancelled", "Inactive", "ApiCancelled"):
        status["log"] = f"keep-cash-usd: order REJECTED immediately (status={st}) -- check " \
                        "the account has Forex trading permissions enabled"
        log.warning("ib_exec: %s", status["log"])
        return status
    status["log"] = (f"keep-cash-usd: SUBMITTED (status={st or 'unknown'}) BUY ${usd_to_buy:,} "
                     f"vs HKD {hkd:,.0f} -- not yet confirmed filled")
    log.info("ib_exec: %s", status["log"])
    return status


# --- idle-cash sweep into SGOV (0-3mo T-bill ETF) --------------------------------
SGOV_SYMBOL = "SGOV"
SGOV_PX_EST = 100.5            # SGOV ~ $100.4 and barely moves; sizing only (MKT fills real)
CASH_SWEEP_TARGET = 0.60      # park 60% of (idle cash + SGOV); keep 40% buffer for the strategy
CASH_SWEEP_MIN_USD = 1500     # don't churn the order for small deltas (anti-churn ONLY -- not a
                              # substitute for CASH_SWEEP_MIN_NAV_USD below; see 2026-07-08 HANDOFF)
CASH_SWEEP_MIN_NAV_USD = 75_000   # per the ADOPTED PLAN: T+1 settlement friction isn't worth it
                                  # on a tiny contribution-fed account -- skip sweeping ENTIRELY
                                  # below this NAV, regardless of the delta-size check above


def _sweep_on() -> bool:
    return os.environ.get("CASH_SWEEP", "").lower() in ("1", "true", "yes")


def sweep_cash() -> dict:
    """Park idle cash in SGOV so it earns ~T-bill yield, keeping a buffer so the
    strategy ALWAYS has cash. Rebalances SGOV toward CASH_SWEEP_TARGET of (idle cash
    + SGOV) each cycle. Paper-guarded + opt-in (CASH_SWEEP=1). Returns a status dict
    for the dashboard. NB: IB *paper* may not credit the actual distribution -- this
    runs the MECHANICS; the real yield materialises on a funded account."""
    status = {"enabled": _sweep_on(), "ok": False, "sgov_qty": 0.0, "sgov_value_base": 0.0,
              "ccy": "", "log": ""}
    if not _sweep_on():
        return status
    ib = _guard()
    if ib is None:
        return status
    acct = ib_client.account_id()          # see mirror_new()'s comment re: Error 435
    contract = ib_client.stock_contract(SGOV_SYMBOL)
    if contract is None:
        status["log"] = "cash-sweep: SGOV contract unavailable, retry"
        return status
    con_id = getattr(contract, "conId", 0)

    def _snap():
        pos = next((p for p in (ib.positions() or []) if p.contract.conId == con_id), None)
        pf = next((i for i in (ib.portfolio() or []) if i.contract.conId == con_id), None)
        qty = float(pos.position) if pos else 0.0
        px = float(pf.marketPrice) if (pf and pf.marketPrice and pf.marketPrice == pf.marketPrice
                                       and pf.marketPrice > 0) else SGOV_PX_EST
        return qty, px
    # READ THE SGOV HOLDING FIRST (robust) so its value is ALWAYS reported, even if the
    # account-summary read below fails on a flaky cycle -- otherwise the dashboard flickers to 0.
    sgov_qty, px = ib_client.call(_snap)
    summ = ib_client.account_summary()
    ccy = (summ or {}).get("_ccy", "") or "HKD"                  # default base = HKD
    status["ccy"] = ccy
    base_per_usd = 1.0 / ib_client._PEG_USD_PER.get(ccy, 1.0)    # USD -> base ccy
    sgov_usd = sgov_qty * px
    status["ok"] = True                                          # SGOV holding read OK
    status["sgov_qty"] = sgov_qty
    status["sgov_value_base"] = sgov_usd * base_per_usd
    if not summ or summ.get("TotalCashValue") is None:           # account unavailable: report
        return status                                            # SGOV value, skip rebalancing
    nav_usd = float(summ.get("NetLiquidation", 0.0) or 0.0) / base_per_usd
    if nav_usd < CASH_SWEEP_MIN_NAV_USD:
        status["log"] = (f"cash-sweep: paused until NAV reaches ${CASH_SWEEP_MIN_NAV_USD:,.0f} "
                         f"(currently ~${nav_usd:,.0f}) -- T+1 friction isn't worth it yet")
        return status                                            # SGOV value already reported above
    cash_usd = float(summ["TotalCashValue"]) / base_per_usd

    # exclude any cash earmarked for a pending manual withdrawal so the sweep does NOT
    # re-buy SGOV with money you're about to take out (and will SELL SGOV to free it).
    reserve_usd = _withdraw_reserve_usd()
    investable = max(cash_usd + sgov_usd - reserve_usd, 0.0)
    if reserve_usd > 0:
        status["log"] = f"(withdrawal reserve ${reserve_usd:,.0f} held aside) "
    target_usd = investable * CASH_SWEEP_TARGET
    delta_usd = target_usd - sgov_usd
    if abs(delta_usd) < CASH_SWEEP_MIN_USD:
        return status
    shares = int(delta_usd // px) if delta_usd > 0 else -int((-delta_usd) // px)
    shares = max(-int(sgov_qty), shares)          # never sell more SGOV than we hold
    if shares == 0:
        return status
    action, qty = ("BUY", shares) if shares > 0 else ("SELL", -shares)
    if os.environ.get("CASH_SWEEP_DRYRUN", "").lower() in ("1", "true", "yes"):
        status["log"] = (f"cash-sweep DRYRUN: would {action} {qty} SGOV @~{px:.2f} "
                         f"(idle ${cash_usd:,.0f} -> target SGOV ${target_usd:,.0f})")
        log.info("ib_exec: %s", status["log"])
        return status
    import ib_async

    def _send():
        o = ib_async.MarketOrder(action, qty)
        o.orderRef = "cash-sweep-sgov"
        o.tif = "DAY"
        if acct:
            o.account = acct
        return ib.placeOrder(contract, o)
    try:
        ib_client.call(_send)
    except Exception as e:                         # noqa: BLE001
        status["log"] = f"cash-sweep: order failed ({e})"
        return status
    status["sgov_qty"] = sgov_qty + (qty if action == "BUY" else -qty)
    status["sgov_value_base"] = status["sgov_qty"] * px * base_per_usd
    status["log"] = (status["log"] + f"cash-sweep: {action} {qty} SGOV @~{px:.2f} "
                     f"(idle cash parked at ~{CASH_SWEEP_TARGET:.0%})")
    log.info("ib_exec: %s", status["log"])
    return status


# --- withdrawal helper: free cash from the CASH SHIELD (SGOV/idle), NEVER the Core book ---
# Why this is minimal (vs the "3-layer lock" pitch): the sweep is STATELESS -- it reads live
# IBKR cash + SGOV every cycle, so a withdrawal can't "desync" a ledger, and the strategy is
# SIGNAL-driven (it never sells Core to raise cash). The ONLY real interaction is the sweep
# re-buying SGOV with cash you've freed for a withdrawal -- so we just earmark a RESERVE the
# sweep excludes. The actual money transfer stays a MANUAL IBKR action (never automated).
WITHDRAW_RESERVE_KEY = "withdraw_reserve_usd"


def _withdraw_reserve_usd() -> float:
    from dashboard.core import store
    try:
        v, _ = store.cache_get(WITHDRAW_RESERVE_KEY)
        return max(float(v), 0.0) if v is not None else 0.0
    except Exception:                              # noqa: BLE001
        return 0.0


def set_withdraw_reserve(amount_usd: float) -> None:
    from dashboard.core import store
    store.cache_set(WITHDRAW_RESERVE_KEY, max(float(amount_usd), 0.0))


def clear_withdraw_reserve() -> None:
    set_withdraw_reserve(0.0)


def prepare_withdrawal(amount_usd: float, dry_run: bool = False) -> dict:
    """Prepare a manual cash withdrawal taking funds from the CASH SHIELD first (idle USD,
    then SGOV), NEVER the Core ETF book. Earmarks `amount_usd` (so the sweep won't re-buy
    SGOV with it) and sells just enough SGOV to cover any shortfall. Does NOT move money out
    -- the actual withdrawal stays a manual IBKR action by design. Paper-guarded. Returns a
    status dict for the UI/CLI. After you withdraw in IBKR, call clear_withdraw_reserve()."""
    out = {"requested_usd": float(amount_usd), "reserved": False, "sgov_sold": 0,
           "idle_usd_after": None, "ready": False, "log": ""}
    if amount_usd <= 0:
        out["log"] = "withdrawal: amount must be > 0"; return out
    ib = _guard()
    if ib is None:
        out["log"] = "withdrawal: not a paper IB connection (guard refused)"; return out
    acct = ib_client.account_id()          # see mirror_new()'s comment re: Error 435
    summ = ib_client.account_summary()
    if not summ or summ.get("TotalCashValue") is None:
        out["log"] = "withdrawal: account read failed, retry"; return out
    ccy = (summ or {}).get("_ccy", "") or "HKD"
    base_per_usd = 1.0 / ib_client._PEG_USD_PER.get(ccy, 1.0)
    idle_usd = float(summ["TotalCashValue"]) / base_per_usd
    contract = ib_client.stock_contract(SGOV_SYMBOL)
    con_id = getattr(contract, "conId", 0) if contract else 0

    def _snap():
        pos = next((p for p in (ib.positions() or []) if p.contract.conId == con_id), None)
        pf = next((i for i in (ib.portfolio() or []) if i.contract.conId == con_id), None)
        qty = float(pos.position) if pos else 0.0
        mp = pf.marketPrice if pf else None
        px = float(mp) if (mp and mp == mp and mp > 0) else SGOV_PX_EST
        return qty, px
    sgov_qty, px = ib_client.call(_snap) if con_id else (0.0, SGOV_PX_EST)

    shortfall = amount_usd - idle_usd
    sell_shares = 0
    if shortfall > 0:                              # need to free cash from the shield (SGOV)
        sell_shares = min(int(shortfall // px) + 1, int(sgov_qty))
    out["idle_usd_after"] = idle_usd + sell_shares * px
    out["ready"] = out["idle_usd_after"] + 1e-6 >= amount_usd
    if not out["ready"]:
        out["log"] = (f"withdrawal SHORT: idle ${idle_usd:,.0f} + sellable SGOV "
                      f"${sgov_qty*px:,.0f} < ${amount_usd:,.0f}. Core is NOT touched -- "
                      f"reduce the amount or top up the cash shield first.")
        return out
    if dry_run:
        out["log"] = (f"withdrawal DRYRUN: would reserve ${amount_usd:,.0f} + sell {sell_shares} "
                      f"SGOV @~{px:.2f} -> idle ~${out['idle_usd_after']:,.0f}; then withdraw "
                      f"manually in IBKR. Core untouched.")
        return out
    set_withdraw_reserve(amount_usd); out["reserved"] = True
    if sell_shares > 0 and contract is not None:
        import ib_async

        def _send():
            o = ib_async.MarketOrder("SELL", sell_shares)
            o.orderRef = "withdraw-sgov"; o.tif = "DAY"
            if acct:
                o.account = acct
            return ib.placeOrder(contract, o)
        try:
            ib_client.call(_send); out["sgov_sold"] = sell_shares
        except Exception as e:                     # noqa: BLE001
            out["log"] = f"withdrawal: SGOV sell failed ({e}); reserve set, sell SGOV manually"
            return out
    out["log"] = (f"withdrawal READY: reserved ${amount_usd:,.0f}, sold {sell_shares} SGOV. "
                  f"Now withdraw ${amount_usd:,.0f} MANUALLY in IBKR, then run --withdraw-clear. "
                  f"Core ETF book untouched.")
    log.info("ib_exec: %s", out["log"])
    return out


def reconcile() -> list[dict]:
    """Per mirrored trade: paper R vs realized IB R from fills (PnL incl.
    commission, normalised by the risk money at placement)."""
    if not ib_client.is_available():
        return []
    ib = ib_client._ensure_conn()
    with paper._LOCK, _conn() as c:
        rows = c.execute("SELECT paper_id, con_id, qty, risk_money, status "
                         "FROM ib_mirror").fetchall()
    journal = {t["id"]: t for t in paper.all_trades()}
    fills = ib_client.call(lambda: list(ib.fills() or []))
    out = []
    for paper_id, con_id, qty, risk_money, status in rows:
        net = sum((f.commissionReport.realizedPNL or 0.0)
                  - (f.commissionReport.commission or 0.0)
                  for f in fills if getattr(f.contract, "conId", None) == con_id
                  and f.commissionReport)
        p = journal.get(paper_id, {})
        out.append({
            "paper_id": paper_id, "instrument": p.get("instrument", "?"),
            "ticket": con_id, "volume": qty, "closed": status == "CLOSED",
            "paper_status": p.get("status", "?"),
            "paper_r": p.get("realized_r", 0.0),
            "demo_r": (net / risk_money) if risk_money else 0.0,
            "demo_pnl": net,
        })
    return out


# ---- helpers ----------------------------------------------------------------

def _parse_expiry(ymd: str) -> dt.date | None:
    ymd = (ymd or "").strip()
    try:
        if len(ymd) >= 8:
            return dt.datetime.strptime(ymd[:8], "%Y%m%d").date()
        if len(ymd) >= 6:
            return dt.datetime.strptime(ymd[:6], "%Y%m").date().replace(day=28)
    except ValueError:
        return None
    return None


def main() -> None:
    import sys
    if "--withdraw-clear" in sys.argv:
        clear_withdraw_reserve(); print("withdraw reserve cleared (back to 0)."); return
    if "--withdraw" in sys.argv:
        i = sys.argv.index("--withdraw")
        amt = float(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 0.0
        res = prepare_withdrawal(amt, dry_run="--dry" in sys.argv)
        for k, v in res.items():
            print(f"  {k}: {v}")
        return
    if not ib_client.is_available():
        print("IB not available (TWS/Gateway running + API enabled?)."); return
    print(f"account={ib_client.account_id()}  paper={is_paper()}")
    rows = reconcile()
    if not rows:
        print("no mirrored trades yet."); return
    print(f"{'id':>4} {'instrument':<8} {'conId':>10} {'closed':>7} "
          f"{'paper':>8} {'paperR':>8} {'ibR':>8} {'pnl':>10}")
    for r in rows:
        print(f"{r['paper_id']:>4} {r['instrument']:<8} {r['ticket']:>10} "
              f"{str(r['closed']):>7} {r['paper_status']:>8} "
              f"{r['paper_r']:>8.2f} {r['demo_r']:>8.2f} {r['demo_pnl']:>10.2f}")


if __name__ == "__main__":
    main()
