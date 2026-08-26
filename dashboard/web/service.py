"""Service layer: holds the live in-memory state and the refresh functions.

Two refresh tiers (this is the whole budget strategy):
  - refresh_cheap(): prices + deterministic scores for ALL instruments. No LLM,
    so it can run as often as the UI asks (every minute is fine).
  - refresh_llm():   one batched board-scan call. Throttled + budget-guarded.
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from analyst.features import compute_facts  # quant/
from analyst import usage_log
from dashboard.instruments import active_universe, active_by_key
from dashboard.data.providers import get_history
from dashboard.core.scoring import score_from_facts, rank, Score
from dashboard.web.news_sources import fetch_headlines
from dashboard.web.board_scan import run_board_scan, InstrumentSignal
from dashboard.data.providers import get_ohlc
from dashboard.core import store
from dashboard.core import paper
from dashboard.core import journal
from dashboard.core import notable_events
from dashboard.core import sleeve
from dashboard.execution import executor
from dashboard.execution import broker          # BROKER-aware dispatch (mt5 executor | ib_exec)
from dashboard.data import mt5_client
from dashboard.core.log import log

# ---- live state (single process, so plain dict is fine) --------------------
STATE: dict = {
    "scores": {},          # key -> Score
    "live": {},            # key -> {price, src, spread}  near-tick when MT5 present
    "spark": {},           # key -> list[float]  short recent close series for mini-charts
    "positions": {},       # paper_id -> live MT5 position (real fill + P&L)
    "llm": {},             # key -> InstrumentSignal
    "macro_note": "",
    "news": [],            # list[str]
    "sources": {},         # key -> provider label
    "last_cheap": None,    # datetime
    "last_llm": None,      # datetime
    "last_status": "not run yet",
    "mt5_available": False,
    "conn": None,          # MT5 connection quality: {server, ping_ms, connected, ...}
    "calls_today": 0,
    "cap": 200,
    "shared_calls_today": 0,       # cross-project (quant+study+events) usage of the shared key
    "shared_calls_by_project": {},
    "pnl_crosscheck": {},          # Layer 2 (2026-07-27): equity-route vs trade-route P&L
                                   # agreement -- see pnl_crosscheck()
}


RECONCILE_PERIODIC_SEC = 600   # 2026-07-21: broker reconciliation (STATE["reconcile"], the
                                # System Health banner's "reconcile:" line) used to run ONLY
                                # on a fresh IB connection -- fine for CATCHING a mismatch
                                # quickly (a fresh connect is exactly when one is likely), but
                                # nothing ever re-ran it on a long-lived connection, so a
                                # since-fixed mismatch could show "mismatch found" forever,
                                # surviving any number of browser refreshes. 10min keeps the
                                # banner honestly current without adding meaningful IB API
                                # load (a plain positions/open-orders diff, not a data fetch).


def reconcile_due(last_ts: dt.datetime | None, now: dt.datetime,
                  periodic_sec: float = RECONCILE_PERIODIC_SEC) -> bool:
    """Should broker reconciliation run now, independent of the fresh-connection trigger
    (checked separately via ib_client.reconcile_needed())? True if it's never run yet, or
    periodic_sec has elapsed since the last run."""
    if last_ts is None:
        return True
    return (now - last_ts).total_seconds() >= periodic_sec


def _now() -> dt.datetime:
    return dt.datetime.now()


def _calibrate_mt5_offset() -> float:
    """MT5 stamps ticks in the broker's SERVER timezone. raw age = now - server
    stamp = real_age - offset, where offset = server lead over UTC (e.g. +3h for
    a UTC+3 broker). A truly fresh tick has real_age ~ 0, so its raw age ~ -offset
    -- i.e. NEGATIVE for a broker ahead of UTC. So we estimate the offset as
    -(most-negative raw age), rounded to 30 min. This handles brokers ahead of
    UTC (the previous version only handled brokers behind, and silently left
    offset=0 -- which fed pre-entry ticks into trade resolution)."""
    raw = [v.get("age") for v in STATE["live"].values()
           if v.get("src") == "mt5-tick" and v.get("age") is not None]
    # the freshest tick has the most-negative raw age; -that ~= the server lead
    cand = -min(raw) if raw else None
    prev, _ = store.cache_get("mt5_offset_sec")
    prev = prev if isinstance(prev, (int, float)) else None
    if cand is not None and -7200 <= cand <= 50400:  # plausible: -2h .. +14h
        off = round(cand / 1800) * 1800
        store.cache_set("mt5_offset_sec", off)
    else:
        off = prev or 0.0
    STATE["mt5_offset_sec"] = off
    # apply correction so 'age' shows true freshness; keep raw for the log
    for v in STATE["live"].values():
        if v.get("src") == "mt5-tick" and v.get("age") is not None:
            v["raw_age"] = v["age"]
            v["age"] = max(0.0, v["age"] + off)  # real_age = raw_age + offset
    return off


def _score_one(inst):
    series, source = get_history(inst)
    if series is None:
        return inst.key, None, source, None, None, None, None, None
    facts, text = compute_facts(series, inst.key)
    score = score_from_facts(inst.key, facts, text)
    # short recent close series for the per-card sparkline (last ~72 bars,
    # rounded + as a plain list to keep the page payload small)
    spark = [round(float(x), 6) for x in series.tail(72)]
    # near-tick live price from MT5 if available, else last bar close (no extra fetch)
    tick = mt5_client.get_tick(inst.mt5)
    if tick is not None:
        live_px, live_src, spread, age = tick["mid"], "mt5-tick", tick["spread"], tick["age_sec"]
    else:
        live_px, live_src, spread, age = float(series.iloc[-1]), source, None, None
    return inst.key, score, source, live_px, live_src, spread, age, spark


# ---- pure sanity-guard logic (no I/O -- unit-testable in isolation) --------------------
def is_nl_implausible(new_nl: float, prev_nl: float | None, lo: float = 0.5, hi: float = 2.0) -> bool:
    """Does a new NetLiquidation reading look wrong relative to the last known-good value?
    False if there's no real baseline yet (prev_nl is None/<=0 -- nothing to compare against,
    so the first-ever reading is always accepted). True if the new value hits zero/negative,
    or moves outside a [lo, hi] x prev_nl band (default: outside 0.5x-2x)."""
    if prev_nl is None or prev_nl <= 0:
        return False
    if new_nl <= 0:
        return True
    return not (lo <= (new_nl / prev_nl) <= hi)


def pending_confirms(pending_val: float | None, new_val: float, tol: float = 0.01) -> bool:
    """Does new_val match a previously-held pending anomaly (within tol)? Explicit
    'is not None' rather than truthiness -- pending_val==0.0 is a legitimate (if rare) value
    to confirm, and treating it as falsy would leave a genuine confirmed drop-to-zero stuck in
    pending limbo forever."""
    return pending_val is not None and abs(pending_val - new_val) < tol


def is_equity_jump_implausible(new_val: float, prev_val: float, gpv: float | None) -> bool:
    """Does a new equity_history reading look like an unexplained jump (a deposit/withdrawal,
    or corrupted data) rather than ordinary trading movement -- i.e. should it be held for
    confirmation before being recorded, and logged as a cash flow (not P&L) once confirmed?

    FOUND 2026-07-10: the original fixed 0.5x-2.0x band only ever caught LARGE jumps -- it
    correctly flagged a ~10x deposit, but would silently miss a routine ~30% monthly
    contribution (the user's actual funding plan) once the account is big enough that the
    contribution is a smaller fraction of NAV, letting it get counted as fake trading P&L.

    Fix: when there are NO open positions (gpv <= ~0), NOTHING legitimate should move
    NetLiquidation beyond tiny interest/FX noise -- so ANY change past a small noise band is
    flagged, catching deposits/withdrawals of any size. With open positions, mark-to-market
    P&L can legitimately swing equity a lot, so fall back to the wider ratio band (a tight
    absolute band would misfire constantly on ordinary position price moves)."""
    if prev_val <= 0:
        return False
    if new_val <= 0:
        return True
    no_positions = gpv is not None and gpv <= 1.0
    if no_positions:
        noise_band = max(100.0, prev_val * 0.005)   # generous vs typical interest/FX noise
        return abs(new_val - prev_val) > noise_band
    return not (0.5 <= new_val / prev_val <= 2.0)


# ---- external cash-flow detection (deposits/withdrawals) -------------------
# ADDED 2026-07-27, after a real HKD 30,000 monthly deposit landed on the LIVE account
# while 9 ETF positions were open and was counted as trading profit (P&L displayed 32,071
# HKD vs a true 2,066 -- a 15x overstatement). is_equity_jump_implausible() above only
# tightens its band when the account is FLAT; with positions open it falls back to the wide
# 0.5x-2.0x ratio band, and 132102/102120 = 1.29 sails straight through. Magnitude alone
# CANNOT work here -- with positions open, a 29% deposit and a 29% market move are
# indistinguishable by size.
#
# The structural signature is unambiguous, because NetLiq = cash + GPV holds exactly
# (verified against the live account: 102,095.55 + 29,968.62 = 132,064.17):
#
#   event         | d_cash | d_gpv  | discriminator
#   --------------|--------|--------|----------------------------------------
#   deposit       |  +X    |  ~0    | cash moves, positions don't
#   withdrawal    |  -X    |  ~0    | cash moves, positions don't
#   buy fill      |  -X    |  +X    | cash <-> positions, they cancel
#   sell fill     |  +X    |  -X    | cash <-> positions, they cancel
#   market move   |   0    |  +/-   | cash untouched
#
# So: cash must move materially (rules out market moves), AND the move must not be
# cancelled by an opposite GPV move (rules out our own fills). Size-independent -- catches
# a 3k deposit as reliably as a 30k one.
CASH_FLOW_MIN_ABS = 1000.0    # floor: below this it's dividends/interest/commission noise,
CASH_FLOW_MIN_PCT = 0.005     # ...or 0.5% of NetLiq, whichever is LARGER. Deliberately set
                              # above a plausible quarterly dividend credit (~500 HKD on a
                              # ~100k book): a dividend is genuine strategy return and must
                              # stay in P&L, NOT be netted out as if it were a deposit.
                              # Layer 2 (broker P&L cross-check) catches whatever slips past.


def hist_cash_gpv(entry) -> tuple[float | None, float | None]:
    """(cash, gpv) from an equity_history entry. Returns (None, None) for the legacy
    3-field [ts, netliq, ccy] entries written before 2026-07-27 -- callers must treat that
    as "can't run the structural check here" and fall back to the magnitude heuristic,
    NOT as "cash was zero"."""
    if len(entry) >= 5 and entry[3] is not None and entry[4] is not None:
        return float(entry[3]), float(entry[4])
    return None, None


def detect_external_cash_flow(prev_cash, prev_gpv, new_cash, new_gpv, netliq,
                              tol: float | None = None) -> float | None:
    """The signed external flow (+deposit / -withdrawal) between two account snapshots, or
    None if this looks like ordinary trading/market movement. See the case table above.

    Returns the CASH delta (not the NetLiq delta) when no simultaneous fill is detected, so
    the amount excludes market drift on open positions -- verified on the 2026-07-27 deposit:
    cash-derived gives exactly 30,000.00, while the NetLiq delta gives 29,981.95 (the -18.05
    difference is real position drift over the 10-min snapshot window, which belongs in P&L,
    not folded into the deposit)."""
    if None in (prev_cash, prev_gpv, new_cash, new_gpv):
        return None                       # legacy entry / broker didn't report the fields
    if tol is None:
        tol = max(CASH_FLOW_MIN_ABS, abs(netliq or 0.0) * CASH_FLOW_MIN_PCT)
    d_cash = new_cash - prev_cash
    d_gpv = new_gpv - prev_gpv
    if abs(d_cash) <= tol:
        return None                       # market-only move: cash untouched
    if abs(d_gpv) <= tol:
        return d_cash                     # no fill -- the cash change IS the external money
    external = d_cash + d_gpv             # a fill moves cash and GPV opposite ways; they
    if abs(external) <= tol:              # cancel, leaving only outside money behind
        return None                       # ...nothing left over: it was purely a trade
    return external


# ---- Layer 2: independent P&L cross-check ---------------------------------
# ADDED 2026-07-27 alongside detect_external_cash_flow(). Layer 1 is still a heuristic on
# broker-reported balances; this is a genuinely INDEPENDENT second opinion, reached by a
# different route entirely, so a misbooked cash flow can't hide in both at once.
#
#   equity route (what the dashboard shows) : deposit-adjusted NetLiq change since tracking
#   trade route  (rebuilt from the journal)  : sum(closed trades' realized $) + broker unrealized
#
# These should agree to within cash interest + dividends + FX drift. A deposit wrongly booked
# as profit inflates ONLY the equity route -- on 2026-07-27 that was a 30,000 HKD gap on a
# 132k account (23%), which this would have surfaced within one tick.
PNL_CROSSCHECK_MIN_ABS = 5000.0    # base-ccy floor; below this the gap is ordinary
PNL_CROSSCHECK_MIN_PCT = 0.05      # interest/dividend/FX accumulation, not a misbooking


def pnl_crosscheck() -> dict:
    """{"ok", "gap", "equity_pl", "trade_pl", "ccy", "tol"} -- or {"ok": None, ...} when
    there isn't enough data to judge (no history, no broker positions, no risk_money rows).
    Never raises: this is a monitoring aid, and a failure here must not disturb a tick."""
    out = {"ok": None, "gap": 0.0, "equity_pl": 0.0, "trade_pl": 0.0, "ccy": "", "tol": 0.0}
    try:
        from dashboard.data import ib_client
        acct = STATE.get("account") or {}
        nl, ccy = acct.get("NetLiquidation"), acct.get("_ccy", "")
        hist, _ = store.cache_get("equity_history")
        hist = paper.with_inception(hist or [])
        flows, _ = store.cache_get("cash_flows")
        if not hist or len(hist) < 2 or nl is None:
            return out
        usd_to_base = 1.0 / ib_client._PEG_USD_PER.get(ccy, 1.0)
        adj = paper.deposit_adjusted_series(hist, flows)
        equity_pl = adj[-1] - adj[0]                       # base ccy, deposits netted out

        # trade route: realized (journal x actual $ risked) + unrealized (broker truth)
        with paper._LOCK, paper._conn() as c:
            risk_by_id = dict(c.execute(
                f"SELECT paper_id, risk_money FROM {broker.mirror_table()}").fetchall())
        if not risk_by_id:
            return out                                     # nothing funded yet -- can't judge
        realized_usd = sum(t["realized_r"] * risk_by_id[t["id"]]
                           for t in paper.all_trades()
                           if t["status"] != "OPEN" and t["id"] in risk_by_id)
        unrealized_usd = sum(p.get("profit", 0.0) for p in (STATE.get("positions") or {}).values())
        trade_pl = (realized_usd + unrealized_usd) * usd_to_base

        gap = equity_pl - trade_pl
        tol = max(PNL_CROSSCHECK_MIN_ABS, abs(nl) * PNL_CROSSCHECK_MIN_PCT)
        out.update({"ok": abs(gap) <= tol, "gap": gap, "equity_pl": equity_pl,
                    "trade_pl": trade_pl, "ccy": ccy, "tol": tol})
    except Exception as e:                                 # noqa: BLE001
        log.debug("pnl_crosscheck error: %s", e)
    return out


# ADDED 2026-07-30, alongside the same-day fix for OPEN positions' stale price (see
# ib_exec.py::live_positions()'s current_price). PENDING (not-yet-funded) signals have no
# broker position to read a live mark from, so they were still falling back to STATE["live"]'s
# WEEKLY-bar price for a pure IB deployment (no MT5) -- the exact same staleness, just for
# trades that haven't been funded yet rather than ones that have. ib_client.get_stock_tick()
# (a live/delayed quote via reqTickersAsync, independent of holding a position) already
# existed and was already used by the sleeve's spread guard, just never for display. Scoped to
# ONLY the instruments with a genuinely pending trade right now (typically 0-5, not the full
# ~22-instrument universe) to avoid extra IBKR API load on instruments nothing is waiting on.
def _refresh_pending_ticks() -> None:
    if not broker.is_ib():
        return   # MT5's STATE["live"] is already tick-fresh via _score_one(); nothing to do
    try:
        from dashboard.core import paper
        pending_keys = {t["instrument"] for t in paper.all_trades()
                        if t["status"] == "OPEN" and t["id"] not in broker.executed_ids()}
    except Exception as e:
        log.debug("_refresh_pending_ticks: could not list pending trades: %s", e)
        return
    if not pending_keys:
        return
    from dashboard.data import ib_client
    _live = dict(STATE["live"])
    got = 0
    for key in pending_keys:
        try:
            tick = ib_client.get_stock_tick(key)
        except Exception as e:
            log.debug("_refresh_pending_ticks: get_stock_tick(%s) failed: %s", key, e)
            continue
        if tick and tick.get("mid"):
            # Same shape _score_one() already writes, so _trade_card()'s existing
            # STATE["live"] fallback needs no changes to pick this up.
            _live[key] = {"price": tick["mid"], "src": "ib-tick",
                          "spread": tick.get("spread"), "age": 0.0}
            got += 1
    STATE["live"] = _live
    # Visibility, not just silent success/failure -- if this account has no real-time
    # market-data subscription for these symbols, get_stock_tick() returns None for
    # every one of them (a normal, already-handled case, not an error) and it's worth
    # being able to SEE that from the log rather than guess.
    log.info("pending-tick refresh: %d/%d instrument(s) got a fresh IB quote (%s)",
             got, len(pending_keys), ", ".join(sorted(pending_keys)))


def _open_position_instruments() -> list:
    """ADDED 2026-08-19: any instrument with a currently-OPEN paper trade that ISN'T in
    the active universe (e.g. a UCITS-swap-retired key like the old EEM/SPY/etc) --
    confirmed live: after the swap + a dashboard restart, an open position on a retired
    key lost its chart/deterministic-facts/Details-dialog content entirely (STATE
    caches are seeded from the PREVIOUS in-memory STATE each cycle, so they only go
    empty on a restart -- the swap alone didn't break anything until this session's own
    Stage 3/live-carryover redeploys cleared them, and a retired key can never be
    re-populated by active_universe() alone again). Still needs live price/chart/facts
    refresh so its trade card doesn't go blank while the position winds down naturally
    -- execution (reconcile/reprotect/close) already worked fine via active_by_key();
    this is the DISPLAY side of that same "keep resolvable" precedent."""
    active_keys = {i.key for i in active_universe()}
    seen: set[str] = set()
    out = []
    for t in paper.all_trades():
        if t["status"] != "OPEN":
            continue
        key = t["instrument"]
        if key in active_keys or key in seen:
            continue
        inst = active_by_key(key)
        if inst is not None:
            out.append(inst)
            seen.add(key)
    return out


def refresh_cheap() -> None:
    """Fetch prices + compute deterministic scores for every instrument."""
    # build into LOCAL dicts, then reassign atomically -- never mutate the live STATE
    # dicts in place, or a UI panel iterating them races ("dict changed size during iteration").
    _sources, _scores, _live, _spark = (dict(STATE["sources"]), dict(STATE["scores"]),
                                        dict(STATE["live"]), dict(STATE["spark"]))
    universe = active_universe() + _open_position_instruments()
    with ThreadPoolExecutor(max_workers=8) as ex:
        for key, score, source, live_px, live_src, spread, age, spark_v in ex.map(_score_one, universe):
            _sources[key] = source
            if score is not None:
                _scores[key] = score
            if live_px is not None:
                _live[key] = {"price": live_px, "src": live_src, "spread": spread, "age": age}
            if spark_v:
                _spark[key] = spark_v
    STATE["sources"], STATE["scores"], STATE["live"], STATE["spark"] = _sources, _scores, _live, _spark
    STATE["mt5_available"] = mt5_client.is_available()
    try:
        _pos = broker.live_positions()                 # None on connection failure
        if _pos is not None:                           # keep last-good on a failed read
            STATE["positions"] = _pos
    except Exception as e:
        log.debug("live_positions error: %s", e)
    _refresh_pending_ticks()
    # broker-agnostic status for the header (computed here so the UI thread never
    # blocks on a broker call). Under BROKER=ib this is the IBKR gateway/account.
    STATE["broker_name"] = broker.name()
    try:
        STATE["broker_conn"] = broker.connection()
    except Exception as e:
        STATE["broker_conn"] = None
        log.debug("broker.connection error: %s", e)
    # ADDED 2026-08-25: cache equity/portfolio-room here too, same reasoning as
    # broker_conn just above ("computed here so the UI thread never blocks on a broker
    # call") -- confirmed via a live faulthandler dump that health_banner() calling
    # broker.equity_usd() directly during main_page()'s render was blocking the ENTIRE
    # uvicorn event loop for up to _run()'s 30s timeout (ib_client.py's Future.result()
    # is a genuine thread-blocking wait, not an await) whenever the gateway was slow or
    # unreachable -- explains the recurring multi-minute "unhealthy" cycles on both
    # paper and live. health_banner() now reads these cached values instead.
    if broker.is_ib():
        try:
            STATE["equity_usd"] = broker.equity_usd()
            STATE["portfolio_room_usd"] = broker.portfolio_room_usd()
        except Exception as e:
            log.debug("equity/portfolio_room cache error: %s", e)
    else:
        STATE["equity_usd"] = None
        STATE["portfolio_room_usd"] = None
    # broker reconciliation: on every FRESH connection (login/reconnect -- see
    # ib_client.reconcile_needed()) PLUS periodically (RECONCILE_PERIODIC_SEC) even on a
    # long-lived, never-reconnecting connection. FIXED 2026-07-21: this used to run ONLY on
    # a fresh connection -- once STATE["reconcile"] recorded a real mismatch (CWB's ghost
    # entry), it stayed showing "mismatch found" in the System Health banner INDEFINITELY
    # (surviving any number of browser refreshes, since a page reload just re-renders
    # whatever STATE currently holds) until the app happened to get ANOTHER fresh IB
    # connection -- which, on a stable connection, could be a very long time. The underlying
    # ghost had already been fixed; the UI just had no way to notice without a lucky/forced
    # reconnect. A periodic re-check keeps this reflecting TRUE current state on its own.
    if broker.is_ib():
        try:
            from dashboard.data import ib_client
            if ib_client.reconcile_needed() or reconcile_due(STATE.get("_reconcile_last_ts"), _now()):
                from dashboard.core import reconcile
                STATE["reconcile"] = reconcile.reconcile_with_broker()
                STATE["_reconcile_last_ts"] = _now()
                ib_client.mark_reconciled()
        except Exception as e:
            log.debug("reconcile error: %s", e)
    # keep last-good account: a momentary gateway/connection hiccup returns None ->
    # don't clobber the cached balances (the panel would flash "data unavailable").
    # SANITY GUARD, confirm-then-accept (2026-07-10, same pattern as equity_history's guard
    # below): "is not None" alone let a genuinely wrong reading straight through -- found live,
    # a second managed account under this login clobbered account_summary()'s output to all
    # zeros (now fixed in ib_client.py, but this is the layer that should have caught the
    # SYMPTOM regardless of which underlying cause produces it next time). A NetLiquidation
    # that suddenly reads implausibly (drops >50% or hits exactly 0 vs the last-good value) is
    # held pending -- STATE["account"] keeps the last-good reading -- and only accepted once
    # the SAME anomalous value repeats on the next cycle (a real, sustained change), same as a
    # transient blip gets silently discarded if the next reading reverts to normal.
    try:
        _acct = broker.account_summary()
        _prev_nl = (STATE.get("account") or {}).get("NetLiquidation")
        if _acct and _acct.get("NetLiquidation") is not None:
            _new_nl = float(_acct["NetLiquidation"])
            if is_nl_implausible(_new_nl, _prev_nl):
                _pending, _ = store.cache_get("account_pending_anomaly")
                _pending_val = _pending.get("val") if _pending else None
                if pending_confirms(_pending_val, _new_nl):
                    STATE["account"] = _acct         # confirmed on 2 consecutive reads -> accept
                    # F3 2026-08-26: record WHEN the broker data was actually read -- /status's
                    # ok-verdict now requires freshness so a cached snapshot can't read green
                    # (found live: a stuck gateway login left the dashboard reporting cached
                    # NAV as ok:true for hours, masking the outage during diagnosis)
                    STATE["acct_ts"] = int(dt.datetime.now().timestamp())
                    store.cache_set("account_pending_anomaly", None)
                    log.warning("account_summary: CONFIRMED sustained change %.2f -> %.2f",
                                _prev_nl, _new_nl)
                else:
                    store.cache_set("account_pending_anomaly", {"val": _new_nl})
                    log.warning("account_summary: implausible NetLiquidation %.2f (prev %.2f) "
                               "-- held pending confirmation, keeping last-good on screen",
                               _new_nl, _prev_nl)
                    # do NOT update STATE["account"] -- last-good value stays displayed
            else:
                store.cache_set("account_pending_anomaly", None)
                STATE["account"] = _acct
                STATE["acct_ts"] = int(dt.datetime.now().timestamp())   # see F3 note above
    except Exception as e:
        log.debug("account_summary error: %s", e)
    # record an equity (NetLiq) snapshot for the portfolio line chart (throttled ~10min)
    try:
        acct = STATE.get("account")
        if acct and acct.get("NetLiquidation") is not None:
            import time as _time
            hist, _ts = store.cache_get("equity_history")
            hist = hist or []
            now_s = int(_time.time())
            new_val = round(float(acct["NetLiquidation"]), 2)
            # SANITY GUARD, confirm-then-accept: a single implausible jump (>50% either way vs
            # the last recorded point) is held as a PENDING candidate rather than recorded or
            # discarded outright. If the NEXT reading confirms the same new level, it's a real,
            # sustained change (a deposit/withdrawal, not a one-off glitch) -- record it AND log
            # the jump as a cash flow so portfolio_panel's Total P&L can net it out (a deposit is
            # not trading profit). If the next reading reverts to the old level instead, the
            # pending candidate is dropped as transient noise.
            # Root-caused 2026-07-02: a stray value of 40 (the LIVE account's balance, ~HKD 1M
            # vs the correct paper value) got recorded here during the mode-isolation bug (now
            # fixed -- see HANDOFF), corrupting both the equity chart and the drawdown-from-peak
            # line at that point. Root-caused again 2026-07-08: the original one-shot-reject
            # version of this guard permanently stuck the chart after a REAL HKD 10,000 deposit,
            # since every future reading was >2x the stale pre-deposit baseline forever.
            # Root-caused a THIRD time 2026-07-10: this check's `new_val > 0` condition meant a
            # drop TO zero/negative was never flagged as implausible at all (it short-circuited
            # to False, skipping the check entirely) -- a genuinely wrong zero reading (the
            # account_summary multi-account bug, see HANDOFF) sailed straight into equity_history
            # unflagged. Now checks the PREVIOUS point's validity instead, so new_val<=0 is
            # explicitly caught. (The account_summary()-level guard above now also blocks a bad
            # zero from ever reaching STATE["account"] in the first place -- this is defense in
            # depth for whatever still gets through, or a genuine real-world case.)
            # Root-caused a FOURTH time 2026-07-10: the fixed 0.5x-2.0x band only ever caught
            # LARGE jumps (it correctly flagged this account's ~10x deposit), but would silently
            # miss a routine ~30% monthly contribution (the user's actual funding plan) once the
            # account is big enough -- letting a real deposit get counted as fake trading P&L.
            # is_equity_jump_implausible() tightens this to catch ANY unexplained change while
            # flat (no open positions -- see its docstring).
            # Root-caused a FIFTH time 2026-07-27: that 07-10 fix only closed the FLAT case;
            # with positions open (the normal state -- this account holds 9-10 ETFs
            # continuously) a real HKD 30,000 deposit still sailed through the wide ratio band
            # and was booked as profit. detect_external_cash_flow() replaces the magnitude
            # guess with the structural cash-vs-position-value signature (see its case table),
            # which is size-independent and works with positions open. The magnitude heuristic
            # stays as a FALLBACK for legacy history entries that predate cash/GPV being
            # recorded, and for any broker that doesn't report those fields.
            _ccy = acct.get("_ccy", "")
            _cash, _gpv = acct.get("TotalCashValue"), acct.get("GrossPositionValue")
            _prev_cash, _prev_gpv = hist_cash_gpv(hist[-1]) if hist else (None, None)
            ext_flow = (detect_external_cash_flow(_prev_cash, _prev_gpv, _cash, _gpv, new_val)
                        if hist else None)
            implausible = bool(hist) and (
                ext_flow is not None
                or is_equity_jump_implausible(new_val, hist[-1][1], _gpv))
            if implausible:
                pending, _pts = store.cache_get("equity_pending_jump")
                _pv = pending.get("val") if pending else None
                # "is not None" not truthiness -- 0 is a legitimate (if rare) value to confirm,
                # and `pending.get("val")` alone treats 0 as falsy = "no pending value", which
                # would leave a genuine confirmed drop-to-zero stuck in pending limbo forever.
                if (_pv is not None and (0.95 <= new_val / _pv <= 1.05 if _pv
                                          else new_val == 0)):
                    flows, _fts = store.cache_get("cash_flows")
                    flows = flows or []
                    # Prefer the cash-derived amount -- it isolates the external money and
                    # leaves market drift in P&L where it belongs (see the function docstring).
                    amount = ext_flow if ext_flow is not None else (new_val - hist[-1][1])
                    flows.append([now_s, round(amount, 2), _ccy])
                    store.cache_set("cash_flows", flows[-500:])
                    hist.append([now_s, new_val, _ccy, _cash, _gpv])
                    store.cache_set("equity_history", hist[-3000:])
                    store.cache_set("equity_pending_jump", None)
                    log.warning("equity_history: CONFIRMED sustained jump %.2f -> %.2f -- "
                               "recorded %+.2f as a cash flow (%s), not P&L",
                               hist[-2][1] if len(hist) > 1 else 0.0, new_val, amount,
                               "cash signature" if ext_flow is not None else "magnitude fallback")
                    notable_events.record(
                        f"cash flow {amount:+,.2f} {_ccy} detected and excluded from P&L "
                        f"({'cash signature' if ext_flow is not None else 'magnitude fallback'})",
                        level="info")
                else:
                    store.cache_set("equity_pending_jump", {"val": new_val, "ts": now_s})
                    log.warning("equity_history: implausible snapshot %.2f (prev %.2f) -- "
                               "held pending confirmation, not recorded yet", new_val, hist[-1][1])
            else:
                store.cache_set("equity_pending_jump", None)  # back to normal: clear any pending
                if not hist or now_s - hist[-1][0] >= 600:
                    hist.append([now_s, new_val, _ccy, _cash, _gpv])
                    store.cache_set("equity_history", hist[-3000:])
    except Exception as e:
        log.debug("equity_history error: %s", e)
    STATE["conn"] = mt5_client.connection_status()
    if STATE["conn"] and STATE["conn"]["ping_ms"] > 300:
        log.warning("MT5 link: %s ping %.0fms (high)", STATE["conn"]["server"],
                    STATE["conn"]["ping_ms"])
    STATE["calls_today"] = store.calls_today()
    _calibrate_mt5_offset()
    # Layer 2 (2026-07-27): independent P&L cross-check -- see pnl_crosscheck(). Runs on the
    # cheap-refresh cadence (all local reads: SQLite + STATE, no broker round-trip), and
    # alerts ONCE per transition into a divergent state rather than every tick.
    try:
        _xc = pnl_crosscheck()
        _was_ok = (STATE.get("pnl_crosscheck") or {}).get("ok")
        STATE["pnl_crosscheck"] = _xc
        if _xc["ok"] is False and _was_ok is not False:
            notable_events.record(
                f"P&L cross-check DIVERGED: equity route {_xc['equity_pl']:+,.0f} vs trade "
                f"route {_xc['trade_pl']:+,.0f} {_xc['ccy']} (gap {_xc['gap']:+,.0f}, "
                f"tolerance {_xc['tol']:,.0f}) -- most likely an unrecorded deposit/withdrawal "
                f"being counted as trading profit; check the Cash flows dialog",
                level="warning")
    except Exception as e:                                 # noqa: BLE001
        log.debug("pnl_crosscheck wiring error: %s", e)
    STATE["last_cheap"] = _now()
    live = STATE["live"]
    n_mt5 = sum(1 for v in live.values() if v.get("src") == "mt5-tick")
    log.info("cheap refresh: %d scored, data source = %s (%d/%d MT5-tick)",
             len(STATE["scores"]),
             "MT5" if n_mt5 else ("yfinance" if live else "none"),
             n_mt5, len(live))
    # ADDED 2026-08-25: MUST run before reprotect_naked_positions()/close_expired_trades()/
    # resolve_open() below -- the inverse gap: a real broker position whose paper_trades row
    # ALREADY resolved (e.g. EXPIRED) without a real closing order ever executing, because
    # close_expired_trades() lost the broker-unreachable race on some earlier cycle and never
    # gets a second chance once status leaves 'OPEN' (see heal_flagged_positions()'s
    # docstring; confirmed live: EEM #27, orphaned during the 2026-08-21..25 dashboard-hang
    # incident). Reopens it with a fresh horizon so the checks below can act on it for real.
    try:
        heal_logs = broker.heal_flagged_positions()
        if heal_logs:
            log.info("flagged-position heal: %d action(s) this refresh", len(heal_logs))
    except Exception as e:
        log.exception("heal_flagged_positions error: %s", e)
    # ADDED 2026-08-18: MUST run before close_expired_trades()/resolve_open() below -- a
    # resting TP/SL bracket can vanish at the broker independent of anything this app does
    # (confirmed live: a paper-gateway session drop lost a sleeve trade's bracket, leaving a
    # real funded position naked for weeks). If that position's price has ALSO already
    # crossed its horizon or its own stored tp/sl, we want the REAL broker-truth close to
    # happen here first, not a price-only inference downstream. See HANDOFF.md 2026-08-18.
    try:
        naked_logs = broker.reprotect_naked_positions()
        if naked_logs:
            log.info("naked-position check: %d action(s) this refresh", len(naked_logs))
    except Exception as e:
        log.exception("reprotect_naked_positions error: %s", e)
    # ADDED 2026-08-17: MUST run before paper.resolve_open() below -- resolve_open() is a
    # pure, broker-independent price/horizon check that can mark a funded core-method trade
    # EXPIRED from OHLC data alone, with no real closing order ever submitted (IBKR has no
    # native time-based auto-close, unlike SL/TP which are real resting broker orders).
    # close_expired_trades() actively closes it for real FIRST, on this same cycle, so
    # resolve_open() never gets to "win the race" against an actual broker action -- mirrors
    # exactly how sleeve.py pads its own horizon_end (TIME_CAP_DAYS*1.5) to guarantee its
    # dynamic exit always fires before resolve_open()'s check would. Confirmed live: without
    # this ordering, this exact race silently orphaned 5 real LIVE + 1 real PAPER position for
    # days -- see HANDOFF.md 2026-08-17.
    try:
        exp_logs = paper.close_expired_trades()
        if exp_logs:
            log.info("closed %d expired core trade(s) this refresh", len(exp_logs))
    except Exception as e:
        log.exception("close_expired_trades error: %s", e)
    # resolve any open paper trades against the fresh price action. Use DAILY
    # bars (covers the multi-week weekly horizon; M1 only spans ~34 days).
    try:
        try:
            _executed_ids = broker.executed_ids()
        except Exception:                          # noqa: BLE001 -- best-effort tag, never block resolution
            _executed_ids = None
        n = paper.resolve_open(lambda inst: get_ohlc(inst, period="1y", interval="1d"),
                               executed_ids=_executed_ids)
        STATE["paper_resolved"] = n
        if n:
            log.info("resolved %d paper trade(s) this refresh", n)
    except Exception as e:
        STATE["paper_resolved"] = f"resolve error: {e}"
        log.exception("paper resolution error: %s", e)
    # keep the demo account in step (close positions whose paper trade resolved)
    try:
        broker.sync_closures()
    except Exception as e:
        log.exception("executor closure sync error: %s", e)
    # Panic-MR sleeve (SLEEVE_ENABLED + Phase-2 equity gated; no-ops otherwise). Runs here
    # (LLM-independent cycle), not the board-scan block -- the sleeve's signal is pure
    # price/VIX/RSI/ADX, no LLM involved. Exits checked BEFORE new entries each cycle.
    try:
        dyn_logs = sleeve.close_expired_sleeves()
        sig_logs = sleeve.place_sleeve_signals(broker.equity_usd())
        if dyn_logs or sig_logs:
            STATE["sleeve_logs"] = dyn_logs + sig_logs
            broker.mirror_new()                       # place the new entry's bracket promptly
    except Exception as e:
        log.exception("sleeve error: %s", e)
    # keep idle cash in USD (opt-in CASH_USD=1): clears the USD margin debit + earns
    # USD interest. Runs BEFORE the SGOV sweep so the debit is cleared first.
    try:
        _fx = broker.keep_cash_usd()                   # keep last-good unless the read succeeded
        if _fx.get("enabled") is False or _fx.get("ok"):
            STATE["fx_usd"] = _fx
    except Exception as e:
        log.debug("keep-cash-usd error: %s", e)
    # park idle cash in SGOV (opt-in CASH_SWEEP=1); strategy always keeps a buffer
    try:
        _cs = broker.sweep_cash()                       # keep last-good unless the read succeeded
        if _cs.get("enabled") is False or _cs.get("ok"):
            STATE["cash_sweep"] = _cs
    except Exception as e:
        log.debug("cash sweep error: %s", e)
    # current short-term T-bill rate (^IRX) = live SGOV-yield proxy; refreshed ~daily
    try:
        import time as _t3
        cached, _ = store.cache_get("tbill_rate")
        if not cached or (_t3.time() - cached[0]) > 14400:   # refresh ^IRX every ~4h
            import yfinance as yf
            irx = yf.download("^IRX", period="5d", interval="1d", progress=False,
                              auto_adjust=True)
            if hasattr(irx.columns, "nlevels") and irx.columns.nlevels > 1:
                irx.columns = irx.columns.get_level_values(0)
            rate = float(irx["Close"].dropna().iloc[-1])
            store.cache_set("tbill_rate", [int(_t3.time()), rate])
            STATE["tbill_rate"] = rate
        else:
            STATE["tbill_rate"] = cached[1]
    except Exception as e:
        log.debug("tbill_rate fetch error: %s", e)
    # SPY benchmark: "am I beating the market" comparison. base_px is a ONE-TIME historical
    # lookup keyed to the account's own tracking-start date (base0_ts from equity_history) --
    # cached forever unless that start date itself changes (a fresh reset). cur_px refreshes
    # on the same ~4h cadence as tbill_rate (no need for anything faster -- a daily-signal
    # strategy doesn't need an intraday-fresh benchmark).
    try:
        hist, _ = store.cache_get("equity_history")
        hist = paper.with_inception(hist or [])
        if hist:
            import time as _t4
            base0_ts = hist[0][0]
            cached_spy, _ = store.cache_get("spy_benchmark")
            need_base = not cached_spy or cached_spy.get("base0_ts") != base0_ts
            need_cur = not cached_spy or (_t4.time() - cached_spy.get("cur_ts", 0)) > 14400
            if need_base or need_cur:
                import yfinance as yf
                import pandas as pd
                spy = yf.download("SPY", period="max", interval="1d", progress=False,
                                  auto_adjust=True)["Close"].dropna()
                if hasattr(spy, "columns"):
                    spy = spy.iloc[:, 0]
                # yfinance's daily index is tz-naive -- strip tz from base_dt too, else
                # pandas raises "Invalid comparison between dtype=datetime64 and datetime".
                base_dt = pd.Timestamp(dt.datetime.fromtimestamp(base0_ts, dt.timezone.utc)
                                        .replace(tzinfo=None))
                idx = spy.index.tz_localize(None) if spy.index.tz is not None else spy.index
                on_or_before = spy[idx <= base_dt]     # dates ascending -> last row = closest
                base_px = float(on_or_before.iloc[-1]) if need_base and len(on_or_before) \
                    else (cached_spy or {}).get("base_px")
                cur_px = float(spy.iloc[-1])
                if base_px:
                    store.cache_set("spy_benchmark", {"base0_ts": base0_ts, "base_px": base_px,
                                                       "cur_px": cur_px, "cur_ts": _t4.time()})
                    STATE["spy_benchmark"] = {"base_px": base_px, "cur_px": cur_px}
                # ADDED 2026-08-26: persist a WEEKLY-SAMPLED SPY close series alongside the
                # two-point benchmark, so the portfolio equity chart can overlay "SPY over
                # the same window" as a real line (not just a single % comparison stat).
                # The period=max download above already happened -- sampling it here is
                # ~free. ~1 point/5 trading days keeps 30y at ~1500 points.
                try:
                    step = max(1, len(spy) // 1500)
                    sampled = spy.iloc[::step]
                    series = [[int(pd.Timestamp(ix).timestamp()), round(float(px), 2)]
                              for ix, px in sampled.items() if pd.notna(px)]
                    if len(series) >= 10:
                        store.cache_set("spy_series", series)
                except Exception as e:
                    log.debug("spy_series sample error: %s", e)
            else:
                STATE["spy_benchmark"] = {"base_px": cached_spy["base_px"],
                                          "cur_px": cached_spy["cur_px"]}
    except Exception as e:
        log.debug("spy_benchmark fetch error: %s", e)
    # SGOV-value history for the dashboard chart (throttled ~10min, same cadence as equity)
    try:
        sv = (STATE.get("cash_sweep") or {}).get("sgov_value_base")
        if sv is not None:
            import time as _t2
            sh, _ = store.cache_get("sgov_history")
            sh = sh or []
            now2 = int(_t2.time())
            if not sh or now2 - sh[-1][0] >= 600:
                sh.append([now2, round(float(sv), 2)])
                store.cache_set("sgov_history", sh[-3000:])
    except Exception as e:
        log.debug("sgov_history error: %s", e)
    # persist a portfolio snapshot so a fresh restart shows last-known stats (not empty).
    # GUARD: only save when we actually have account data -- never overwrite a good snapshot
    # with an empty one from a cycle where the broker connection wasn't ready yet.
    try:
        if STATE.get("account") and STATE["account"].get("NetLiquidation") is not None:
            import time as _t4
            store.cache_set("portfolio_snapshot", {
                "ts": int(_t4.time()),
                "account": STATE.get("account"), "positions": STATE.get("positions"),
                "cash_sweep": STATE.get("cash_sweep"), "fx_usd": STATE.get("fx_usd"),
                "tbill_rate": STATE.get("tbill_rate"),
                "spy_benchmark": STATE.get("spy_benchmark"),
                "broker_name": STATE.get("broker_name"),
                "broker_conn": STATE.get("broker_conn")})
    except Exception as e:
        log.debug("portfolio_snapshot save error: %s", e)


def refresh_news() -> None:
    STATE["news"] = fetch_headlines()


def refresh_llm(cap: int | None = None) -> str:
    """Run the batched board scan if budget allows. Returns a status string."""
    cap = cap or STATE["cap"]
    scores = list(STATE["scores"].values())
    if not scores:
        return "no data yet -- run a cheap refresh first"
    ranked = rank(scores)
    result, status = run_board_scan(ranked, STATE["news"], cap=cap)
    if result is not None:
        STATE["llm"] = {s.key: s for s in result.signals}
        STATE["macro_note"] = result.macro_note
        STATE["last_llm"] = _now()
        # append the FULL scan to the audit journal (the cache only keeps the
        # latest; this preserves the whole history for retrospective)
        try:
            journal.record_scan(result, STATE["scores"])
        except Exception as e:
            log.warning("journal: could not record board scan: %s", e)
        # cache a lightweight snapshot so a restart shows something immediately
        store.cache_set("last_board_scan", {
            "macro_note": result.macro_note,
            "signals": [s.model_dump() for s in result.signals],
        })
        # turn the fresh signals into forward paper trades (both SL/TP methods)
        try:
            STATE["paper_logs"] = paper.place_from_state(STATE)
        except Exception as e:
            STATE["paper_logs"] = [f"placement error: {e}"]
        # mirror new live-variant trades to the MT5 DEMO account (real fills);
        # executor refuses to act unless the account is broker-flagged demo
        try:
            STATE["executor_logs"] = broker.mirror_new()
        except Exception as e:
            STATE["executor_logs"] = [f"executor error: {e}"]
            log.exception("executor mirror error: %s", e)
    STATE["last_status"] = status
    STATE["calls_today"] = store.calls_today()
    try:
        shared = usage_log.fetch_shared_usage_today()
        STATE["shared_calls_today"] = shared["calls"]
        STATE["shared_calls_by_project"] = shared["calls_by_project"]
    except Exception as e:
        log.warning("shared usage fetch failed: %s", e)
    log.info("LLM board scan: %s (calls today %d/%d)", status, STATE["calls_today"], cap)
    return status


def heal_series(hist: list, lo: float = 0.5, hi: float = 2.0) -> tuple[list, list]:
    """PURE function (no I/O -- unit-testable in isolation): given a [[ts, val, ccy], ...]
    series, return (cleaned, removed). Detects a run of consecutive points that deviates
    outside [lo, hi] x the last known-good value (or hits <=0) AND is later bracketed by a
    clean return to that same normal level -- exactly the shape of the 2026-07-10 incident (and
    the earlier 'stray 40' one). DELIBERATELY conservative: a run that ISN'T yet bracketed by a
    return to normal (still ongoing / unconfirmed, e.g. sitting at the end of the series) is
    left untouched -- this can never delete a genuine ongoing change, only already-resolved
    glitches. See test_service.py for the scenarios this is checked against."""
    cleaned: list = []
    removed: list = []
    i, n = 0, len(hist)
    while i < n:
        prev_good = cleaned[-1][1] if cleaned else None
        v = hist[i][1]
        if prev_good and prev_good > 0 and (v <= 0 or not (lo <= v / prev_good <= hi)):
            j = i
            while j < n and (hist[j][1] <= 0 or not (lo <= hist[j][1] / prev_good <= hi)):
                j += 1
            if j < n and lo <= hist[j][1] / prev_good <= hi:
                removed.extend(hist[i:j])             # bracketed by a return to normal -> drop
                i = j
                continue
            # NOT bracketed (run extends to the end of history, unconfirmed) -- leave it;
            # the confirm-then-accept guard governs whether it's real, not this audit
        cleaned.append(hist[i])
        i += 1
    return cleaned, removed


def _self_heal_equity_history() -> None:
    """I/O wrapper around heal_series(): loads equity_history, heals it, saves back if
    anything changed. This is a RETROACTIVE audit, complementary to the confirm-then-accept
    guard in refresh_cheap() (which stops NEW bad points from being WRITTEN going forward) --
    this catches anything already sitting in stored history, whether from before that guard
    existed (2026-07-10's 45-point cleanup, done manually, was exactly this pattern) or from
    some future bug the guard doesn't cover. Runs on every page load (restore_cache()),
    throttled to once per ~10min so rapid page refreshes don't re-scan the whole series
    repeatedly."""
    import time as _time
    last_scan, _ = store.cache_get("equity_healed_ts")
    now_s = _time.time()
    if last_scan and now_s - last_scan < 600:
        return
    hist, _ = store.cache_get("equity_history")
    if hist and len(hist) >= 3:
        cleaned, removed = heal_series(hist)
        if removed:
            store.cache_set("equity_history", cleaned)
            log.warning("equity_history: self-heal removed %d anomalous point(s), e.g. %s",
                        len(removed), removed[0])
    store.cache_set("equity_healed_ts", now_s)


def restore_cache() -> None:
    """Load the last board scan + portfolio snapshot from disk on startup (no broker
    call) so the dashboard shows last-known stats immediately instead of an empty section."""
    try:
        _self_heal_equity_history()
    except Exception as e:                              # noqa: BLE001
        log.debug("equity self-heal error: %s", e)
    data, ts = store.cache_get("last_board_scan")
    if data:
        STATE["macro_note"] = data.get("macro_note", "")
        STATE["llm"] = {s["key"]: InstrumentSignal(**s) for s in data.get("signals", [])}
        STATE["last_status"] = f"restored cached scan from {ts}"
    # portfolio snapshot: only fill keys the live refresh hasn't populated yet
    snap, _sts = store.cache_get("portfolio_snapshot")
    if snap:
        for k in ("account", "cash_sweep", "fx_usd", "tbill_rate", "spy_benchmark",
                  "broker_name", "broker_conn"):
            if snap.get(k) is not None and not STATE.get(k):
                STATE[k] = snap[k]
        pos = snap.get("positions")
        if pos and not STATE.get("positions"):
            STATE["positions"] = {int(k): v for k, v in pos.items()}   # JSON str keys -> int
        if STATE.get("last_cheap") is None and snap.get("ts"):
            STATE["portfolio_ts"] = snap["ts"]                         # data-as-of for the UI
        # F3 2026-08-26: restored account data is CACHE -- stamp it with the snapshot's age
        # so /status can tell "live broker read" from "restored last-known" apart.
        if snap.get("ts") and not STATE.get("acct_ts"):
            STATE["acct_ts"] = int(snap["ts"])
