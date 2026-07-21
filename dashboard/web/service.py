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
from dashboard.instruments import active_universe
from dashboard.data.providers import get_history
from dashboard.core.scoring import score_from_facts, rank, Score
from dashboard.web.news_sources import fetch_headlines
from dashboard.web.board_scan import run_board_scan, InstrumentSignal
from dashboard.data.providers import get_ohlc
from dashboard.core import store
from dashboard.core import paper
from dashboard.core import journal
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


def refresh_cheap() -> None:
    """Fetch prices + compute deterministic scores for every instrument."""
    # build into LOCAL dicts, then reassign atomically -- never mutate the live STATE
    # dicts in place, or a UI panel iterating them races ("dict changed size during iteration").
    _sources, _scores, _live, _spark = (dict(STATE["sources"]), dict(STATE["scores"]),
                                        dict(STATE["live"]), dict(STATE["spark"]))
    with ThreadPoolExecutor(max_workers=8) as ex:
        for key, score, source, live_px, live_src, spread, age, spark_v in ex.map(_score_one, active_universe()):
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
    # broker-agnostic status for the header (computed here so the UI thread never
    # blocks on a broker call). Under BROKER=ib this is the IBKR gateway/account.
    STATE["broker_name"] = broker.name()
    try:
        STATE["broker_conn"] = broker.connection()
    except Exception as e:
        STATE["broker_conn"] = None
        log.debug("broker.connection error: %s", e)
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
            implausible = bool(hist) and is_equity_jump_implausible(
                new_val, hist[-1][1], acct.get("GrossPositionValue"))
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
                    flows.append([now_s, new_val - hist[-1][1], acct.get("_ccy", "")])
                    store.cache_set("cash_flows", flows[-500:])
                    hist.append([now_s, new_val, acct.get("_ccy", "")])
                    store.cache_set("equity_history", hist[-3000:])
                    store.cache_set("equity_pending_jump", None)
                    log.warning("equity_history: CONFIRMED sustained jump %.2f -> %.2f -- "
                               "recorded as a cash flow, not P&L", hist[-2][1] if len(hist) > 1
                               else 0.0, new_val)
                else:
                    store.cache_set("equity_pending_jump", {"val": new_val, "ts": now_s})
                    log.warning("equity_history: implausible snapshot %.2f (prev %.2f) -- "
                               "held pending confirmation, not recorded yet", new_val, hist[-1][1])
            else:
                store.cache_set("equity_pending_jump", None)  # back to normal: clear any pending
                if not hist or now_s - hist[-1][0] >= 600:
                    hist.append([now_s, new_val, acct.get("_ccy", "")])
                    store.cache_set("equity_history", hist[-3000:])
    except Exception as e:
        log.debug("equity_history error: %s", e)
    STATE["conn"] = mt5_client.connection_status()
    if STATE["conn"] and STATE["conn"]["ping_ms"] > 300:
        log.warning("MT5 link: %s ping %.0fms (high)", STATE["conn"]["server"],
                    STATE["conn"]["ping_ms"])
    STATE["calls_today"] = store.calls_today()
    _calibrate_mt5_offset()
    STATE["last_cheap"] = _now()
    live = STATE["live"]
    n_mt5 = sum(1 for v in live.values() if v.get("src") == "mt5-tick")
    log.info("cheap refresh: %d scored, data source = %s (%d/%d MT5-tick)",
             len(STATE["scores"]),
             "MT5" if n_mt5 else ("yfinance" if live else "none"),
             n_mt5, len(live))
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
