"""NiceGUI dashboard: real-time trade analysis for Gold, Oil and FX.

Runs TWO independent instances of this same app: PAPER (IBKR paper account) and
LIVE (IBKR real-money account, IB_ALLOW_LIVE=1). Both places real broker orders
automatically from qualifying signals -- this is NOT decision-support-only; a
human doesn't confirm each trade before it's sent to the broker.

Run:  python -m dashboard.app      (then open http://localhost:8080)

Refresh model (two tiers, to respect the daily API cap):
  - cheap tier (prices + deterministic scores): runs at the selected interval.
  - LLM board scan: one batched call, throttled to >=10 min and budget-guarded.
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401  -- TLS bootstrap first

import asyncio
import datetime as dt
import os
import signal
import faulthandler
from nicegui import app, ui, run

# ADDED 2026-08-21: diagnostic for the still-unresolved intermittent dashboard hang (both
# paper and live cycle "unhealthy" every ~2-3min per the docker-watchdog log, root cause
# unknown despite multiple fix attempts -- see HANDOFF). `py-spy dump` was the natural next
# diagnostic but is blocked: Docker containers don't grant SYS_PTRACE by default, and
# `docker exec -u root ... py-spy dump` still failed with Permission denied (confirmed
# directly) -- adding that capability means recreating the live container, a bigger step
# than needed. `faulthandler` needs no special capability: it's stdlib, runs IN-process, and
# SIGUSR1 dumps every thread's current stack straight to stderr (captured by `docker logs`)
# with zero extra dependencies. Guarded by hasattr since SIGUSR1 doesn't exist on native
# Windows (this same file also runs there during local dev).
if hasattr(signal, "SIGUSR1"):
    faulthandler.register(signal.SIGUSR1, all_threads=True)

# Mode MUST be resolved before importing anything that touches the DB (service -> paper/store
# compute their DB path at IMPORT time from DASH_DB_NAME). `store` itself is lightweight/self-
# contained (stdlib only), so it's safe to import this early.
from dashboard.core import store

# Extracted to core/mode.py (2026-08-13, same pattern as core/resilient_loop.py) so the
# DASH_DB_NAME-preservation property has an actual regression test -- this file can't be
# imported in a test itself (`ui.run()` at module level blocks).
from dashboard.core.mode import resolve_mode as _resolve_mode


DASH_MODE = _resolve_mode()

from dashboard.web import service                          # noqa: E402 -- AFTER mode resolution
from dashboard.instruments import BY_KEY, active_by_key     # noqa: E402
from dashboard.core.scoring import rank                     # noqa: E402

# ---- settings (live, editable from the UI) --------------------------------
# cheap_min: prices/scores/trade-resolution interval (deterministic, free).
# llm_min:   LLM macro/news scan interval (independent; slow-moving, budgeted).
SETTINGS = {"cheap_min": 1, "llm_min": 15, "auto_pause": True,
            "cap": 200, "grid_cols": 4, "chart_period": "All", "chart_scale": "Truncated",
            "chart_view": "P&L (ex-deposits)",
            # ADDED 2026-08-05: Active Trades card sort control. "desc" is defined as
            # "biggest/most-recent first" for EVERY key (newest entry date, highest R, highest
            # profit, largest invested amount) -- one consistent mental model instead of
            # per-key direction semantics.
            "active_sort": "entry_date", "active_sort_dir": "desc",
            "alerts_filter": "all", "density": "comfortable"}
# label -> sort-key value, in the order shown in the dropdown
ACTIVE_SORT_KEYS = {"entry_date": "Entry date", "r": "Unrealized R",
                    "profit": "Profit", "invested": "Invested amount"}
CHART_PERIODS = {"1W": 7, "1M": 30, "3M": 90, "All": None}   # label -> lookback days (None = all)
_busy = {"flag": False}


def _save_settings() -> None:
    """Persist UI settings so they survive a restart (the watchdog relaunches fresh)."""
    try:
        from dashboard.core import store
        from dashboard.core import paper as _p
        store.cache_set("ui_settings", {
            "cheap_min": SETTINGS["cheap_min"], "llm_min": SETTINGS["llm_min"],
            "auto_pause": SETTINGS["auto_pause"], "cap": SETTINGS["cap"],
            "grid_cols": SETTINGS["grid_cols"], "chart_period": SETTINGS["chart_period"],
            "chart_scale": SETTINGS["chart_scale"], "chart_view": SETTINGS["chart_view"],
            "active_sort": SETTINGS["active_sort"], "active_sort_dir": SETTINGS["active_sort_dir"],
            # "unread" is NOT a valid ALERTS_FILTERS key -- this stale default is what put an
            # unusable value into ui_settings in the first place (see alerts_panel's own
            # 2026-08-26 note). Keep this in sync with ALERTS_FILTERS.
            "alerts_filter": SETTINGS.get("alerts_filter", "all"),
            "density": SETTINGS.get("density", "comfortable"),
            "trades_filter": SETTINGS.get("trades_filter", "all"),
            "trades_search": SETTINGS.get("trades_search", ""),
            "risk_per_trade": _p.RISK_PER_TRADE,
            "overext_filter": _p.OVEREXT_FILTER, "overext_hi": _p.OVEREXT_HI,
            "tech_paused": _p.TECH_PAUSED})
    except Exception:                                  # noqa: BLE001 -- settings are non-critical
        pass


def _load_settings() -> None:
    """Restore persisted UI settings at startup (applied to SETTINGS + paper globals)."""
    try:
        from dashboard.core import store
        from dashboard.core import paper as _p
        saved, _ts = store.cache_get("ui_settings")
        if not saved:
            return
        for k in ("cheap_min", "llm_min", "auto_pause", "cap", "grid_cols", "chart_period",
                 "chart_scale", "chart_view", "active_sort", "active_sort_dir",
                 "alerts_filter", "density", "trades_filter", "trades_search"):
            if k in saved:
                SETTINGS[k] = saved[k]
        if "risk_per_trade" in saved:
            _p.RISK_PER_TRADE = float(saved["risk_per_trade"])
        if "overext_filter" in saved:
            _p.OVEREXT_FILTER = bool(saved["overext_filter"])
        if "overext_hi" in saved:
            _p.OVEREXT_HI = float(saved["overext_hi"])
            _p.OVEREXT_LO = float(100 - saved["overext_hi"])
        if "tech_paused" in saved:
            _p.TECH_PAUSED = bool(saved["tech_paused"])
        # else: leaves TECH_PAUSED at its module-level default (True as of 2026-07-30) --
        # correct for the FIRST deploy after this setting was added, where no prior value
        # has ever been saved yet.
    except Exception:                                  # noqa: BLE001
        pass


_load_settings()                                       # apply persisted settings at import


# ---- helpers ---------------------------------------------------------------

def _market_open(now: dt.datetime | None = None) -> bool:
    """Guard for the LLM auto-pause -- the only call site (_do_llm() below). Renamed in
    spirit but kept this name (only one caller, avoid a pointless rename): as of 2026-07-31
    this also checks INTRADAY hours (9:30am-3:30pm ET), not just weekday, per explicit user
    request -- purely to avoid burning LLM API budget analyzing signals outside real trading
    hours, a different concern from the old rationale below (which was about ORDER-PLACEMENT
    safety, irrelevant here: it doesn't matter that a hypothetical order would sit unfilled
    or get rejected outside hours -- the point is not to spend API calls on signals nobody
    can act on for hours anyway). Cuts 30min before the 4pm close (not the full session) --
    also user-specified, gives the analysis a buffer before end-of-day volatility/spread
    widening rather than analyzing right up to the bell. **CHANGED 2026-07-31 (same day):**
    now also excludes US market holidays via `market_calendar.is_us_trading_day()` (real
    NYSE calendar, not a hand-maintained list -- see that module's docstring) -- superseding
    the earlier "not worth a maintained holiday calendar" call now that the user asked for
    it explicitly and a real (self-maintaining) calendar package was added instead of a
    fixed list. `now` param (ET-aware if passed) is for direct unit testing without mocking
    datetime.now() globally.

    FIXED 2026-07-11 (weekday check, still applies): this box's system clock is
    Asia/Hong_Kong (UTC+8), 12h ahead of US Eastern (the market this account actually
    trades, BROKER=ib/UNIVERSE=etf -- NYSE-listed ETFs). Using the LOCAL weekday meant HK
    Sat 00:00-04:00 (still Fri 12:00-16:00 ET, regular trading hours) was wrongly treated as
    closed, and HK Mon 00:00-21:30 (still Sun noon - Mon pre-market ET) was wrongly treated
    as open -- roughly half a day of misalignment at each week boundary. Confirmed live: the
    auto-pause kicked in at HK Sat 00:00:14, which was Fri 12:00pm ET -- cutting off the
    rest of Friday's real trading session. For the MT5/FX legacy path (~24h market, no
    single relevant exchange timezone) local weekday is kept as-is, no hours check.

    **CHANGED 2026-08-19 for the UCITS instrument swap (paper first)**: `refresh_llm()`
    scans the WHOLE board in one batch, not per-instrument, so this can't just switch to a
    single exchange's window the way ib_exec.py's per-trade
    `within_entry_execution_window()` does -- the active universe now genuinely spans BOTH
    NYSE (9:30am-3:30pm ET) and LSE (8:00am-4:00pm UK, ~3:00am-11:00am ET) sessions at once,
    so this returns True if EITHER is open (an OR of both windows), not just NYSE. Before
    this fix the gate was NYSE-only, so the LLM would sit paused (and blind to LSE-hours
    signals) for the ~5.5h/day LSE trades before NYSE opens. The actual OR-of-both-sessions
    logic lives in market_calendar.us_lse_market_open() -- extracted rather than inlined so
    it has a real regression test (app.py itself can't be imported in a test, `ui.run()` at
    module level blocks -- same reason resolve_mode() was extracted to core/mode.py)."""
    from dashboard.instruments import _ib_broker
    if _ib_broker():
        from dashboard.core.market_calendar import us_lse_market_open
        return us_lse_market_open(now)
    else:
        now = now or dt.datetime.now()
        return now.weekday() < 5  # Mon-Fri, no intraday check (MT5 path, unchanged)


def _ago(t: dt.datetime | None) -> str:
    if t is None:
        return "never"
    secs = (dt.datetime.now() - t).total_seconds()
    if secs < 60:
        return "< 1 min ago"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


SIG_COLOR = {"BUY": "positive", "SELL": "negative", "WAIT": "grey", "WATCH": "grey-6"}

# Backtest-measured SIGNAL frequency (not fill frequency -- see _fundable_count below for
# why those two differ at small account sizes). From the 21-ETF live universe, 33.4y:
# `BROKER=ib UNIVERSE=etf python -m dashboard.research.backtest --longweekly`
# (2026-07-08): "PORTFOLIO TRADES ... FREQUENCY: ~38 trades/year | ~0.7/week". A long-run
# average, not a promise -- actual weeks cluster (several signals at once in a strong
# synchronized trend, or none for a stretch). Re-measure if the universe changes again.
BACKTEST_SIGNAL_FREQ_YR = 38
BACKTEST_SIGNAL_FREQ_WK = 0.7

# FIXED 2026-07-23: this used to be hardcoded -10.5% directly in portfolio_panel() (3 places)
# -- a leftover from the OLD 0.5%-risk-era plan figures, never updated when the deployed
# config moved to risk=1%/pos_cap=30%/gate+sleeve. That's a real accuracy bug, not just
# staleness: showing a stale (too-generous) reference line means a live drawdown could sit
# "under the line" on the chart while already exceeding what the CURRENT strategy's own
# backtest has ever produced, understating how bad "worse than backtest" actually looks.
# Current figure: deployed config's FULL 32-year-history max drawdown (core + reclaim-1.0R-
# buffer gate + panic-MR sleeve, risk=1%, pos_cap=30%) -- see README.md's "Update 2026-07-18"
# section / HANDOFF.md for the full table. Re-measure and update this if the deployed config
# changes again (a new gate variant, a pos_cap/risk change, a new universe member, etc.).
BACKTEST_MAX_DD_PCT = -8.83

# ADDED 2026-08-06, user-requested (a "behavioral anchor" for the worst-case recovery wait):
# from dashboard/research/drawdown_time_stats.py's "core+gate+sleeve@10% (deployed config)"
# full-history run -- 376 TRADING days, trough to next new equity high. NOTE: that script
# uses the SAME simpler additive-blend methodology as full_live_config_retest.py, not the
# byte-for-byte joint-sizing method BACKTEST_MAX_DD_PCT (-8.83%) above was measured with --
# directionally comparable, not a like-for-like reproduction (see that script's own
# docstring for why). Converted trading days -> CALENDAR days (x7/5) so it's directly
# comparable to the live dashboard's own days-underwater count below, which is measured in
# real elapsed calendar time (hist timestamps), not trading days.
BACKTEST_MAX_RECOVERY_DAYS = round(376 * 7 / 5)   # ~526 calendar days, ~17-18 months


# ---- ADDED 2026-08-26: shared UI helpers (freshness badges + asset-class map) ------------

def _freshness_label(ts: dt.datetime | None, warn_min: int = 90, bad_min: int = 360,
                     prefix: str = "updated"):
    """One standardized data-freshness badge: grey when fresh, amber past warn_min,
    red past bad_min. Replaces the ad-hoc 'updated X ago' labels that each chose their
    own staleness behavior -- one component means every panel's 'how stale is this'
    reads the same way. Returns the label element so callers can chain .tooltip()."""
    fresh = _ago(ts)
    cls = "text-xs"
    if ts is None:
        cls += " text-grey-6"
    else:
        age_min = (dt.datetime.now() - ts).total_seconds() / 60.0
        if age_min >= bad_min:
            cls += " text-red-7 font-bold"
        elif age_min >= warn_min:
            cls += " text-orange-8 font-bold"
        else:
            cls += " text-grey-6"
    return ui.label(f"{prefix} {fresh}").classes(cls)


# Coarse asset-class grouping for the exposure-by-class bar: instruments.py's fine-grained
# classes rolled up to the buckets the README's universe description actually uses.
_COARSE_CLASS = {
    "metal": "Metals", "index": "Equity", "intl_eq": "Equity", "china_eq": "Equity",
    "rate": "Rates",
    "credit": "Credit", "convertible": "Credit", "preferred": "Credit", "em_bond": "Credit",
    "commodity": "Commodities", "mlp": "Commodities", "energy": "Commodities",
    "reit": "REITs", "intl_reit": "REITs", "inflation": "Inflation",
}


def _asset_class_for(symbol: str | None) -> str | None:
    """Map an instrument key/broker symbol to its coarse asset class (None if unknown --
    unknown gets its own honest 'Other' bucket rather than being silently dropped)."""
    if not symbol:
        return None
    sym = str(symbol).strip().upper()
    sym = sym.split()[0].split(".")[0]          # "IGLN.L" -> "IGLN", "GOLD Aug26" -> "GOLD"
    try:
        from dashboard import instruments as _inst
        for lookup in ("ETF_BY_KEY", "ETF_CANDIDATE_BY_KEY", "ETF_TRADED_BY_KEY"):
            inst = getattr(_inst, lookup).get(sym)
            if inst:
                return _COARSE_CLASS.get(inst.asset_class, inst.asset_class.title())
        inst = BY_KEY.get(sym) or getattr(_inst, "FUT_BY_KEY", {}).get(sym)
        if inst:
            return _COARSE_CLASS.get(inst.asset_class, inst.asset_class.title())
        for lookup in ("_RETIRED_ETF_UNIVERSE",):
            retired = getattr(_inst, lookup, None) or []
            for inst2 in retired:
                if inst2.key == sym:
                    return _COARSE_CLASS.get(inst2.asset_class, inst2.asset_class.title())
    except Exception:                                  # noqa: BLE001 -- never break rendering
        return None
    return None


# HKT is the user's wall clock (UTC+8, no DST) -- used for every timestamp the
# user sees. Containers run in UTC; relying on system-local `astimezone()` would show
# the wrong wall time there. One definition here ensures every display path is consistent.
HKT = dt.timezone(dt.timedelta(hours=8))


# ---- refreshable panels ----------------------------------------------------

def _fmt_ts(s: str) -> str:
    """Format a stored timestamp for display in HKT.
    Stored values are UTC (the canonical form); we convert to HKT here."""
    if not s:
        return "—"
    try:
        d = dt.datetime.fromisoformat(str(s))
    except Exception:
        return str(s).replace("T", " ")[:16]
    if d.tzinfo is not None:        # UTC-aware -> HKT wall time
        d = d.astimezone(HKT)
    elif d.tzinfo is None:
        # naive timestamps in this codebase are UTC by convention
        d = d.replace(tzinfo=dt.timezone.utc).astimezone(HKT)
    return d.strftime("%Y-%m-%d %H:%M") + " HKT"


def _fmt_age(secs: float) -> str:
    s = abs(secs)
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s/60:.0f}m"
    if s < 172800:
        return f"{s/3600:.0f}h"
    return f"{s/86400:.0f}d"


def _data_source_text() -> tuple[str, str]:
    """Return (label, css) describing the live price source / broker connection."""
    from dashboard.execution import broker
    live = dict(service.STATE.get("live") or {})        # snapshot: avoid iterating a live dict
    if broker.is_ib():
        ib_live = {k: v for k, v in live.items() if v.get("src") == "ib-tick"}
        n = len(ib_live)
        if n:
            return (f"Data: IBKR ● {n}/{len(live)} live ticks", "text-green font-bold")
        return ("Data: yfinance ○ delayed  (IBKR: no real-time mkt-data sub — "
                "weekly signals run fine on delayed/historical)", "text-grey-6")
    mt5_live = {k: v for k, v in live.items() if v.get("src") == "mt5-tick"}
    if mt5_live:
        ages = [v["age"] for v in mt5_live.values() if v.get("age") is not None]
        newest = f", newest tick {_fmt_age(min(ages))}" if ages else ""
        # age is now broker-offset-corrected (true freshness), so a tight
        # threshold is safe: fresh weekday tick ~0; weekend/stalled feed grows.
        stale = bool(ages) and min(ages) > 3600
        off = service.STATE.get("mt5_offset_sec", 0) or 0
        offtxt = f"  (broker clock +{off/3600:.0f}h)" if off else ""
        return (f"Data: MT5 ● {len(mt5_live)}/{len(live)} live{newest}{offtxt}"
                + ("  — market closed/feed stale" if stale else ""),
                "text-orange font-bold" if stale else "text-green font-bold")
    if service.STATE.get("mt5_available"):
        return ("Data: yfinance ○ delayed  (MT5 connected but no symbol match — "
                "fix names in instruments.py)", "text-orange font-bold")
    return ("Data: yfinance ○ delayed  (MT5 not connected)", "text-grey-6")


@ui.refreshable
def clock_row() -> None:
    now_utc = dt.datetime.now(dt.timezone.utc)
    hkt = now_utc.astimezone(HKT)
    parts = [f"HKT {hkt:%H:%M:%S} (UTC+8)", f"UTC {now_utc:%H:%M:%S}"]
    from dashboard.execution import broker as _bk
    off = service.STATE.get("mt5_offset_sec", 0) or 0
    # FIXED 2026-07-24: this used to ALWAYS append a third "Broker UTC (IBKR)" entry for the
    # IB path -- but it never showed an actual time, just that static label, since IB
    # timestamps ARE UTC (no offset). A third always-visible clock that's 100% redundant with
    # the UTC one right next to it (both this project's deployed instances use BROKER=ib) --
    # dropped the visible entry, folded the same fact into a tooltip on the UTC clock instead.
    # The MT5 branch is unchanged -- that one shows a genuinely DIFFERENT broker time.
    _utc_tip = None
    if _bk.is_ib():
        _utc_tip = "IBKR broker timestamps are UTC too -- no separate offset to show"
    elif service.STATE.get("mt5_available") and off:
        bkt = now_utc + dt.timedelta(seconds=off)
        parts.append(f"Broker {bkt:%H:%M:%S} (UTC{off/3600:+.0f})")
    else:
        parts.append("Broker — (MT5 offset not detected)")
    with ui.row().classes("items-center gap-4"):
        for i, p in enumerate(parts):
            lbl = ui.label(p).classes("text-xs font-mono "
                                      + ("text-green-8" if i == 2 and off else "text-grey-7"))
            if i == 1 and _utc_tip:
                lbl.tooltip(_utc_tip)


@ui.refreshable
def header_status() -> None:
    """2026-07-24: this used to render ~4 stacked lines on every page load -- data source,
    timestamps, LLM budget, account P&L -- all useful occasionally, none of them the "is my
    money okay" signal that has to be visible at a glance (that's this function's one
    remaining line -- broker connection -- plus health_banner() right below it). The rest
    moved into the ⓘ info modal next to the title (_open_info_modal())."""
    from dashboard.execution import broker as _broker
    if _broker.is_ib():
        bc = service.STATE.get("broker_conn") or {}
        up = bc.get("available")
        ok = bc.get("ok")
        dot = "●" if up else "○"
        css = ("text-green" if up and ok else "text-orange" if up
               else "text-red")
        ui.label(f"{_broker.name()}: {bc.get('detail', 'gateway down')} {dot}")\
            .classes(f"text-sm {css}")\
            .tooltip("BROKER=ib — orders go to the IBKR paper account (guard requires a "
                     "DU… paper account on a paper port), or the LIVE account when "
                     "IB_ALLOW_LIVE is armed (guard requires the exact configured "
                     "live account on a live port)")
        return
    conn = service.STATE.get("conn")
    if conn:
        from dashboard.execution import link_monitor
        lk = link_monitor.status()
        ap = lk.get("access_point") or conn["server"]
        ping = lk.get("ping_ms") or conn["ping_ms"]
        dot = "●" if conn["connected"] else "○"
        css = ("text-green" if conn["connected"] and ping < 150
               else "text-orange" if conn["connected"] and ping < 300
               else "text-red")
        ui.label(f"MT5: {ap} · {ping:.0f}ms {dot}")\
            .classes(f"text-sm {css}")\
            .tooltip(f"server {conn['server']}; retransmission "
                     f"{conn['retransmission']:.0%}; "
                     f"seen: {lk.get('history', {})}")


def _format_duration(delta: dt.timedelta) -> str:
    """"Xd Yh Zm" for a countdown -- e.g. market open/close -- dropping leading zero units
    (a 20-minute countdown reads as "20m", not "0d 0h 20m"). Negative/zero deltas (the target
    moment already passed, e.g. a stale cache during the exact transition second) floor to
    "0m" rather than showing a confusing negative duration."""
    total_min = max(0, int(delta.total_seconds() // 60))
    days, rem_min = divmod(total_min, 24 * 60)
    hours, minutes = divmod(rem_min, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:              # show 0h once a day-figure is already present, for readability
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


@ui.refreshable
def health_banner() -> None:
    """ADDED 2026-07-14: a single at-a-glance row for 'is anything actually wrong', after
    this session found several real issues that were each individually invisible until
    someone happened to check the right sub-panel or time a request by hand (a -89.8% fake
    drawdown display, a response-time regression from ~2s to 5-8s, a position-mismatch false
    alarm). None of these are NEW checks -- every value here already existed in STATE
    somewhere; this just aggregates them into one scannable place instead of requiring a
    tour of the whole Board to notice something's off."""
    now = dt.datetime.now()

    def _age_txt(ts) -> tuple[str, str]:
        if ts is None:
            return "never", "text-grey-5"
        age_s = (now - ts).total_seconds()
        txt = f"{age_s:.0f}s ago" if age_s < 120 else f"{age_s/60:.0f}m ago"
        return txt, "text-grey-5"

    dur = service.STATE.get("last_tick_duration_sec")
    tick_ts = service.STATE.get("last_tick_ts")
    tick_txt, _ = _age_txt(tick_ts)
    # thresholds calibrated against this session's own regression: ~2-3s is the normal
    # baseline for this app, ~5-8s is exactly what the per-card broker-call bug produced
    dur_colour = ("text-grey-6" if dur is None else
                  "text-green" if dur < 3 else "text-orange" if dur < 8 else "text-red")
    dur_txt = "n/a" if dur is None else f"{dur:.1f}s"

    cheap_txt, _ = _age_txt(service.STATE.get("last_cheap"))
    llm_txt, _ = _age_txt(service.STATE.get("last_llm"))
    # ADDED 2026-08-04: a growing "llm: 3h ago" reads as broken even when it's the
    # market-hours auto-pause correctly holding it back (confirmed live: user reported
    # "LLM call seems not running" at 05:49am ET, 3h40m before the 9:30am open -- the gate
    # was working exactly as designed, but nothing in this compact status line said so, so
    # a stale-looking age was the only visible signal). Append a short reason when the most
    # recent skip was specifically the auto-pause, so this reads as "expected, not broken"
    # without having to go check the info modal or HANDOFF.md.
    if (SETTINGS.get("auto_pause") and
            "market closed (auto-pause)" in (service.STATE.get("last_status") or "")):
        llm_txt += " (paused, outside hours)"

    # ADDED 2026-08-05, user-requested: time until the next real NYSE open/close, right next
    # to the cheap/llm row above that it directly explains (that row's "(paused, outside
    # hours)" hint said WHY nothing was happening; this says how much longer). Deliberately
    # REAL exchange hours (9:30am-4:00pm ET, exact per-day incl. early closes), not this
    # system's own narrower 10:00am-3:30pm ET entry-execution window -- "market open/close"
    # should mean what it says. US-equity path only (IB) -- MT5 forex trades ~24/5, "market
    # hours" isn't a meaningful concept there, mirroring _market_open()'s own scoping.
    from dashboard.execution import broker as _mkt_bk
    market_txt, market_colour = None, "text-grey-5"
    if _mkt_bk.is_ib():
        from dashboard.core import market_calendar
        ms = market_calendar.market_status()
        if ms["next_change"]:
            delta = ms["next_change"] - dt.datetime.now(dt.timezone.utc)
            market_txt = (("open · closes in " if ms["is_open"] else "closed · opens in ")
                          + _format_duration(delta))
            market_colour = "text-green" if ms["is_open"] else "text-grey-5"

    # ADDED 2026-08-06, user-requested "behavioral anchor": how many days into the CURRENT
    # drawdown (if any), vs the historical worst case (BACKTEST_MAX_RECOVERY_DAYS above) -- so
    # a long flat/underwater stretch reads as "expected, within the documented worst case"
    # instead of "the system might be broken", the exact failure of nerve trend-following's
    # own literature warns is the real risk (not the drawdown itself). Same hist/flows source
    # portfolio_panel()'s own drawdown chart uses (paper.drawdown_series()), fetched
    # independently here since health_banner() renders earlier on the page and doesn't
    # otherwise share state with that later panel.
    from dashboard.core import paper as _dd_paper, store as _dd_store
    _hist, _ = _dd_store.cache_get("equity_history")
    _hist = _dd_paper.with_inception(_hist or [])
    _flows, _ = _dd_store.cache_get("cash_flows")
    dd_dur_txt, dd_dur_colour, dd_dur_tooltip = None, "text-grey-5", ""
    if len(_hist) >= 2:
        _dd_series = _dd_paper.drawdown_series(_hist, _flows)
        _cur_dd = _dd_series[-1] if _dd_series else 0.0
        if _cur_dd >= -0.05:      # essentially at/within noise of the all-time peak
            dd_dur_txt, dd_dur_colour = "at new high", "text-green"
            dd_dur_tooltip = ("Deposit-adjusted equity is at (or within 0.05% of) its "
                              "all-time peak -- no active drawdown right now.")
        else:
            # walk backward to the most recent "at a new high" point in TRACKED history --
            # if tracking started mid-drawdown, this understates the true age (the real peak
            # predates what's tracked), disclosed in the tooltip rather than silently assumed.
            _peak_i = 0
            _found_peak = False
            for _i in range(len(_dd_series) - 1, -1, -1):
                if _dd_series[_i] >= -0.05:
                    _peak_i = _i; _found_peak = True
                    break
            _days = (_hist[-1][0] - _hist[_peak_i][0]) / 86400.0
            # ADJUSTED 2026-08-06: threshold moved to 60% of the record (was an arbitrary
            # 80%/30d split) per a user-reviewed critique's specific reasoning -- 60% is
            # early enough to prompt a REVIEW (not an automatic size cut, see the "does NOT
            # apply" list in HANDOFF's parameter-freeze entry) while still well short of the
            # record, giving genuine lead time rather than only flagging once already close.
            _dd_alert_days = BACKTEST_MAX_RECOVERY_DAYS * 0.6
            dd_dur_colour = ("text-red" if _days >= _dd_alert_days else
                             "text-orange" if _days >= 30 else "text-grey-5")
            dd_dur_txt = f"{_days:.0f}d underwater (record: ~{BACKTEST_MAX_RECOVERY_DAYS}d)"
            dd_dur_tooltip = (
                f"Days since the deposit-adjusted equity curve last touched its all-time "
                f"peak. The backtest's own worst case for this exact config (core+gate+"
                f"sleeve@10%, full 30y history) was ~{BACKTEST_MAX_RECOVERY_DAYS} calendar "
                "days (~18 months) trough-to-new-high -- staying under that isn't a promise "
                "it can't take longer, but it's the documented reference point, not a guess. "
                f"Flagged red past {_dd_alert_days:.0f}d (60% of the record) as a prompt to "
                "review, not an automatic size cut -- see the parameter-freeze policy."
                + ("" if _found_peak else " NOTE: tracked history never shows an earlier new "
                                          "high, so this is a lower bound -- the real peak "
                                          "may predate when tracking began."))

    # ADDED 2026-08-06, user-requested: a warning when filled + pending exposure is
    # approaching PORTFOLIO_CAP, so the pressure is visible BEFORE it hits the hard gate
    # (which just silently scales/skips new entries) rather than only being discoverable by
    # noticing several "retrying" pending cards at once. Reuses the SAME cached
    # account_summary() read active_panel()'s own eq/room already use -- calling this a
    # second time here doesn't add a broker round-trip (ib_client.account_summary() has its
    # own short TTL cache, confirmed by reading it directly), just reads the shared result.
    from dashboard.execution import broker as _cap_bk
    cap_txt, cap_colour, cap_tooltip = None, "text-grey-5", ""
    if _cap_bk.is_ib():
        # ADDED 2026-08-25: read the cache service.refresh_cheap() now populates instead of
        # calling equity_usd()/portfolio_room_usd() live here -- this function runs
        # synchronously inside main_page()'s HTTP render path, and a live call could block
        # the whole event loop for up to ib_client._run()'s 30s timeout when the gateway is
        # slow/unreachable (confirmed via a live faulthandler thread dump -- this exact call
        # chain was caught blocking the main uvicorn thread, explaining the recurring
        # multi-minute dashboard-unresponsive cycles on both paper and live).
        _cap_eq = service.STATE.get("equity_usd")
        _cap_room = service.STATE.get("portfolio_room_usd")
        if _cap_eq and _cap_room is not None:
            _cap_total = _cap_eq * float(os.environ.get("PORTFOLIO_CAP", "1.0"))
            _cap_used_pct = (1 - _cap_room / _cap_total) if _cap_total > 0 else 0.0
            if _cap_used_pct >= 0.90:
                cap_colour = "text-red" if _cap_used_pct >= 0.98 else "text-orange"
                cap_txt = f"{_cap_used_pct*100:.0f}% of PORTFOLIO_CAP committed"
                cap_tooltip = (
                    "Filled positions (GrossPositionValue) plus pending (not-yet-filled) "
                    "broker orders, as a fraction of equity x PORTFOLIO_CAP. Only ~"
                    f"USD {_cap_room:,.0f} of room remains -- new signals will scale down or "
                    "get held back (see Active Trades' 'retrying' cards) until room frees up "
                    "or the cap changes. Informational; the hard gate already enforces this "
                    "regardless of whether you see this warning.")

    bc = service.STATE.get("broker_conn") or {}
    broker_ok = bc.get("ok")
    broker_colour = "text-green" if broker_ok else "text-orange" if bc else "text-red"
    broker_txt = bc.get("detail", "no broker") if bc else "not connected"

    rec = service.STATE.get("reconcile") or {}
    rec_tooltip = None
    if rec.get("only_local") or rec.get("only_broker"):
        rec_colour, rec_txt = "text-red", "mismatch found"
        # 2026-07-23: this used to ALSO render as a separate "⚠ position mismatch" badge in
        # header_status() -- same STATE["reconcile"], same event, shown twice at once. That
        # duplication is exactly what made a since-fixed mismatch confusing to read: two
        # indicators to check, and no guarantee they'd read consistently. Consolidated into
        # this single "reconcile:" line (the one place `_refresh_all_panels()` already treats
        # as the System Health summary); the detailed symbol-level tooltip moves here too so
        # nothing is lost by dropping the second badge.
        rec_tooltip = (
            f"Broker reconciliation (run on last login, or periodically -- see "
            f"RECONCILE_PERIODIC_SEC) found a desync -- "
            f"local-only (ghost, no broker position): {rec.get('only_local')}; "
            f"broker-only (no local record): {rec.get('only_broker')}. "
            f"Check ib_mirror vs paper_trades and the broker's own position list "
            f"directly before trusting P&L numbers.")
    elif rec.get("skipped"):
        rec_colour, rec_txt = "text-grey-5", "skipped (broker unavailable)"
    elif rec:
        rec_colour, rec_txt = "text-green", "matched"
    else:
        rec_colour, rec_txt = "text-grey-5", "no check yet this session"

    # ADDED 2026-07-27 (Layer 2): P&L cross-check -- see service.pnl_crosscheck(). This is the
    # line that would have caught the 30,000 HKD deposit being booked as profit, within a tick.
    xc = service.STATE.get("pnl_crosscheck") or {}
    if xc.get("ok") is True:
        xc_colour, xc_txt = "text-green", "agrees"
    elif xc.get("ok") is False:
        xc_colour, xc_txt = "text-red", f"diverged {xc['gap']:+,.0f} {xc['ccy']}"
    else:
        xc_colour, xc_txt = "text-grey-5", "not enough data"
    xc_tooltip = (
        "Two independent routes to the same number: deposit-adjusted equity change "
        f"({xc.get('equity_pl', 0):+,.0f}) vs the trade journal's realized $ plus broker "
        f"unrealized ({xc.get('trade_pl', 0):+,.0f}), tolerance {xc.get('tol', 0):,.0f} "
        f"{xc.get('ccy', '')}. They should agree to within cash interest, dividends and FX. "
        "A large gap usually means a deposit or withdrawal was counted as trading profit -- "
        "record it via Cash flows to correct it.")

    with ui.row().classes("w-full items-center gap-4 bg-grey-1 rounded px-3 py-1"):
        ui.label("System health").classes("text-xs uppercase text-grey-7 font-bold")
        with ui.row().classes("items-baseline gap-1"):
            ui.label("tick:").classes("text-xs text-grey-6")
            ui.label(f"{tick_txt} ({dur_txt})").classes(f"text-xs {dur_colour}").tooltip(
                "When the last tick cycle ran, and how long it took. ~2-3s is normal for "
                "this app; several seconds slower with no other symptom is exactly what a "
                "past regression here looked like (excess per-request broker calls).")
        with ui.row().classes("items-baseline gap-1"):
            ui.label("cheap/llm:").classes("text-xs text-grey-6")
            ui.label(f"{cheap_txt} / {llm_txt}").classes("text-xs text-grey-5")
        if market_txt:
            with ui.row().classes("items-baseline gap-1"):
                ui.label("market:").classes("text-xs text-grey-6")
                from zoneinfo import ZoneInfo
                # HKT via a fixed offset, not .astimezone() (system-local) -- matches
                # analyst/usage_log.py's existing convention (its own HKT constant's
                # docstring: HKT has no DST, always UTC+8, no zoneinfo/tzdata dependency
                # needed) rather than trusting the Windows host's local-tz name resolution.
                _hkt = dt.timezone(dt.timedelta(hours=8))
                _et = ms["next_change"].astimezone(ZoneInfo("America/New_York"))
                _hkt_t = ms["next_change"].astimezone(_hkt)
                ui.label(market_txt).classes(f"text-xs {market_colour}").tooltip(
                    f"Real NYSE regular hours (not this system's own narrower "
                    f"10:00am-3:30pm ET entry-execution window) -- "
                    f"{'closes' if ms['is_open'] else 'opens'} "
                    f"{_et.strftime('%a %I:%M%p ET')} ({_hkt_t.strftime('%a %I:%M%p HKT')}). "
                    "Holidays and early-close days are already accounted for.")
        if dd_dur_txt:
            with ui.row().classes("items-baseline gap-1"):
                ui.label("drawdown:").classes("text-xs text-grey-6")
                ui.label(dd_dur_txt).classes(f"text-xs {dd_dur_colour}").tooltip(dd_dur_tooltip)
        if cap_txt:
            with ui.row().classes("items-baseline gap-1"):
                ui.label("capacity:").classes("text-xs text-grey-6")
                ui.label(cap_txt).classes(f"text-xs {cap_colour}").tooltip(cap_tooltip)
        with ui.row().classes("items-baseline gap-1"):
            ui.label("broker:").classes("text-xs text-grey-6")
            ui.label(broker_txt).classes(f"text-xs {broker_colour}")
        with ui.row().classes("items-baseline gap-1"):
            ui.label("reconcile:").classes("text-xs text-grey-6")
            rec_label = ui.label(rec_txt).classes(f"text-xs {rec_colour}")
            if rec_tooltip:
                rec_label.tooltip(rec_tooltip)
        with ui.row().classes("items-baseline gap-1"):
            ui.label("P&L check:").classes("text-xs text-grey-6")
            ui.label(xc_txt).classes(f"text-xs {xc_colour}").tooltip(xc_tooltip)


@ui.refreshable
def macro_banner() -> None:
    note = service.STATE.get("macro_note") or "Run an LLM scan for a macro read."
    with ui.card().classes("w-full bg-blue-1"):
        ui.label("Macro backdrop").classes("text-xs uppercase text-grey-7")
        ui.label(note).classes("text-sm")


def _sparkline_svg(series: list[float], up: bool, w: int = 240, h: int = 40) -> str:
    """Tiny inline-SVG price sparkline. Green if the window closed up, red if
    down. No axes/labels — a glance, not a chart. Cheap enough for 14 cards."""
    if not series or len(series) < 2:
        return ""
    lo, hi = min(series), max(series)
    rng = (hi - lo) or 1.0
    n = len(series)
    pad = 3
    def _x(i): return pad + i * (w - 2 * pad) / (n - 1)
    def _y(v): return pad + (h - 2 * pad) * (1 - (v - lo) / rng)
    pts = " ".join(f"{_x(i):.1f},{_y(v):.1f}" for i, v in enumerate(series))
    color = "#21ba45" if up else "#db2828"
    # faint area fill under the line + the line itself + a dot at the last point
    area = f"{pad},{h-pad} " + pts + f" {w-pad},{h-pad}"
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f'<polygon points="{area}" fill="{color}" opacity="0.10"/>'
        f'<polyline points="{pts}" fill="none" stroke="{color}" '
        f'stroke-width="1.5" vector-effect="non-scaling-stroke"/>'
        f'<circle cx="{_x(n-1):.1f}" cy="{_y(series[-1]):.1f}" r="2.2" fill="{color}"/>'
        f'</svg>')


def _pending_keys() -> set:
    """Instrument keys with an OPEN journal trade that was never actually mirrored to
    the broker (see active_panel's confirmed/pending split). Computed once per panel
    render and passed into _signal_card, rather than re-querying per card."""
    from dashboard.core import paper
    positions = service.STATE.get("positions", {})
    return {t["instrument"] for t in paper.open_trades() if not positions.get(t["id"])}


def _signal_card(key: str, compact: bool = False, width_class: str = "min-w-[260px] grow",
                 pending_keys: set | None = None):
    score = service.STATE["scores"].get(key)
    sig = service.STATE["llm"].get(key)
    inst = active_by_key(key)
    # LLM action wins for display if present, else deterministic signal
    action = sig.action if sig else (score.signal if score else "—")
    conf = f"{sig.confidence:.0%}" if sig else ""
    live = service.STATE.get("live", {}).get(key)
    price = live["price"] if live else (score.facts["last_price"] if score else None)
    src = live["src"] if live else service.STATE["sources"].get(key, "")
    with ui.card().classes(f"{width_class} h-full"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.row().classes("items-baseline gap-1"):
                ui.label(f"{inst.name}").classes("text-base font-bold")
                ui.label(key).classes("text-xs text-grey-6 font-mono")
            with ui.row().classes("items-center gap-1"):
                if score:
                    from dashboard.core import paper
                    scol = ("positive" if score.strength >= paper.MIN_STRENGTH
                            else ("orange" if score.strength == paper.MIN_STRENGTH - 1
                                  else "grey"))
                    ui.badge(f"{score.strength}/5", color=scol)\
                        .props("outline").classes("text-xs").tooltip(
                            f"trend strength (need ≥{paper.MIN_STRENGTH} to trade)")
                ui.badge(action, color=SIG_COLOR.get(action, "grey")).classes("text-sm")
        if pending_keys and key in pending_keys:
            ui.badge("⏳ PENDING", color="grey-7").classes("text-xs").tooltip(
                "A signal fired and was logged, but never got sized/placed on the "
                "broker (e.g. account too small) -- this is not a real position.")
        if price is not None:
            with ui.row().classes("items-baseline gap-2"):
                ui.label(f"{price:,.4f}").classes("text-lg")
                tag = "● live" if src == "mt5-tick" else "○ delayed"
                tcolor = "text-green" if src == "mt5-tick" else "text-grey-5"
                ui.label(tag).classes(f"text-xs {tcolor}")
        spark = service.STATE.get("spark", {}).get(key)
        if spark:
            up = spark[-1] >= spark[0]
            ui.html(_sparkline_svg(spark, up, h=32 if compact else 40))\
                .classes("w-full")
        if score:
            ui.label(score.note).classes("text-xs text-grey-7")
        if sig:
            ui.label(f"LLM: {sig.bias} ({conf}) — {sig.rationale}").classes("text-xs")
            if not compact:
                ui.label(f"Invalid if: {sig.invalidation}").classes("text-xs text-grey-6 italic")
        ui.button("Details", on_click=lambda k=key: _open_detail(k)).props("flat dense").classes("text-xs")


def _top_opportunity_keys() -> list[str]:
    """Keys shown in Top Opportunities (most-obvious BUY/SELL, top 4). Shared so
    the Other-instruments grid can exclude them and not show duplicates."""
    scores = rank(list(service.STATE["scores"].values()))
    return [s.key for s in scores if s.signal in ("BUY", "SELL")][:4]


@ui.refreshable
def opportunities() -> None:
    scores = rank(list(service.STATE["scores"].values()))
    top = set(_top_opportunity_keys())
    obvious = [s for s in scores if s.key in top]
    ui.label("Top Opportunities (most obvious trends)").classes("text-lg font-bold")
    if not obvious:
        ui.label("No obviously aligned trends right now — mostly WATCH/WAIT.").classes("text-sm text-grey")
        return
    pending = _pending_keys()
    n = SETTINGS.get("grid_cols", 3)
    with ui.element("div").classes("w-full items-stretch").style(
            f"display:grid; grid-template-columns: repeat({n}, minmax(0,1fr)); gap:0.75rem;"):
        for s in obvious:
            _signal_card(s.key, compact=True, width_class="w-full", pending_keys=pending)


@ui.refreshable
def grid() -> None:
    n = SETTINGS.get("grid_cols", 3)
    top = set(_top_opportunity_keys())  # don't repeat the highlighted ones
    others = [s for s in rank(list(service.STATE["scores"].values())) if s.key not in top]
    if not others:
        return
    # 2026-07-23: this used to be its own "text-lg font-bold" heading -- same visual weight
    # as "Top Opportunities" above it, for what's really the SAME ranked list continuing past
    # a cutoff, not a separate concept. A plain divider + small caption keeps the distinction
    # (still visually obvious where "top" ends) without a second full heading.
    ui.separator().classes("my-2")
    ui.label("Other instruments (ranked, not currently a top signal)")\
        .classes("text-xs text-grey-6 uppercase")
    pending = _pending_keys()
    # inline CSS grid (not Tailwind grid-cols-N, which Tailwind purges when the
    # column count is dynamic) so any chosen column count always renders.
    with ui.element("div").classes("w-full items-stretch").style(
            f"display:grid; grid-template-columns: repeat({n}, minmax(0,1fr)); gap:0.75rem;"):
        for s in others:
            _signal_card(s.key, width_class="w-full", pending_keys=pending)


@ui.refreshable
def gate_panel() -> None:
    """Per-instrument gate breakdown: why each signal does or doesn't trade."""
    from dashboard.core import paper
    rows_data = paper.gate_report(service.STATE)
    ui.label("Signal gate status — why a trade does / doesn't fire")\
        .classes("text-lg font-bold")
    gates = ["BUY/SELL", "confluence",
             f"objective edge ≥ {paper.MIN_EDGE_R:+.2f}R",
             f"strength ≥ {paper.MIN_STRENGTH}/5"]
    if paper.VOL_FILTER:
        gates.append("vol ≥ median")
    gates += [f"R:R ≥ {paper.MIN_RR}", "cooldown clear", "de-correlation clear"]
    ui.label("Every instrument scored against the live entry gates (need: "
             + " · ".join(gates) + "). 'edge' = empirical expectancy of this "
             "regime (strength × vol) from the confidence model. "
             "Sorted most-obvious first.")\
        .classes("text-xs text-grey-6")
    if not rows_data:
        ui.label("No scores yet — waiting for the first refresh.").classes("text-sm text-grey")
        return
    _badge = {"WOULD TRADE": "🟢 would trade", "OPEN": "🔵 open",
              "BLOCKED": "🔴 blocked"}
    # hide WAIT/WATCH instruments -- only show directional candidates
    rows = [{
        "instrument": f"{active_by_key(r['key']).name} ({r['key']})",
        "key": r["key"],
        "action": r["action"],
        "strength": f"{r['strength']}/5",
        "edge": (f"{r['obj_edge']:+.2f}R (n{r['obj_n']})"
                 if r["obj_edge"] is not None else "—"),
        "vol": "ok" if r["vol_ok"] else "low",
        "status": _badge.get(r["status"], r["status"]),
        # an OPEN position's re-entry gates are irrelevant -- don't list them
        "blocked by": ("—" if r["status"] == "OPEN"
                       else "; ".join(r["blocked_by"]) or "—"),
        "detail": "",
    } for r in rows_data if r["status"] != "WAIT"]
    if not rows:
        ui.label("No directional candidates right now — all instruments are "
                 "WAIT/WATCH.").classes("text-sm text-grey")
        return
    cols = [c for c in rows[0] if c != "key"]
    with ui.element("div").classes("w-full overflow-x-auto rounded border"):
        gtable = ui.table(rows=rows,
                 columns=[{"name": c, "label": "" if c == "detail" else c,
                           "field": c,
                           "align": "left" if c in ("blocked by", "status", "instrument") else "center",
                           "sortable": c in ("instrument", "strength", "edge", "status")}
                          for c in cols])\
            .classes("w-full min-w-[720px]").props("dense flat pagination=15")
        gtable.add_slot("body-cell-detail", '''
            <q-td :props="props">
                <q-btn flat dense size="sm" icon="info" color="primary"
                       @click="() => $parent.$emit('detail', props.row.key)" />
            </q-td>
        ''')
        gtable.on("detail", lambda e: _open_detail(e.args))


def _active_universe_keys() -> set[str]:
    from dashboard.instruments import active_universe
    return {i.key for i in active_universe()}


def _open_detail(key: str) -> None:
    score = service.STATE["scores"].get(key)
    sig = service.STATE["llm"].get(key)
    # RESPONSIVE FIX 2026-07-15: was "min-w-[520px]" with no ceiling -- min-width always
    # wins over max-width when they conflict (CSS spec), so this would force a 520px-wide
    # dialog on a 375px phone, clipped/overflowing. w-[92vw] scales down with the viewport;
    # max-w-[Npx] caps it on desktop, and the two never conflict since there's no min-width.
    with ui.dialog() as dlg, ui.card().classes("w-[92vw] max-w-[520px]"):
        ui.label(active_by_key(key).name).classes("text-xl font-bold")
        ui.label(f"Source: {service.STATE['sources'].get(key,'?')}").classes("text-xs text-grey")
        ui.separator()
        ui.label("Deterministic facts").classes("font-bold text-sm")
        if score:
            ui.markdown("```\n" + score.facts_text + "\n```")
        if sig:
            ui.separator()
            ui.label("LLM view").classes("font-bold text-sm")
        elif key not in _active_universe_keys():
            # ADDED 2026-08-19: a retired instrument (e.g. an old UCITS-swap key still
            # winding down an open position) never gets a FRESH LLM view again --
            # refresh_llm() is deliberately NOT extended to retired keys the way
            # refresh_cheap() was (service.py::_open_position_instruments()), since an
            # already-open position isn't making a new entry decision that needs one,
            # and LLM calls cost real API budget. Say so explicitly rather than leaving
            # the space blank with no explanation, which read as broken.
            ui.separator()
            ui.label("LLM view: not available -- this instrument was retired from "
                    "active scanning; its open position is winding down on its own "
                    "SL/TP, no new LLM analysis runs for it.").classes(
                "text-xs text-grey-6 italic")
            ui.label(f"{sig.action} · {sig.bias} · confidence {sig.confidence:.0%}")
            ui.label(sig.rationale).classes("text-sm")
            # ADDED 2026-07-14: explicit check of whether a macro theme the LLM already
            # identified (in the board's own macro_note) actually applies to THIS
            # instrument -- previously only existed implicitly (or not at all) in the
            # free-text rationale; see board_scan.py's InstrumentSignal.macro_linkage.
            if getattr(sig, "macro_linkage", None):
                ui.label(f"Macro linkage: {sig.macro_linkage}").classes(
                    "text-sm text-blue-8")
            ui.label(f"Invalidation: {sig.invalidation}").classes("text-sm text-grey-7")
        ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()


def _sortable_cols(names: list[str]) -> list[dict]:
    """Column defs for ui.table with click-to-sort enabled on every column (Quasar's
    native QTable sort, asc/desc/none) -- ADDED 2026-08-19, user-requested."""
    return [{"name": c, "label": c, "field": c, "sortable": True} for c in names]


@ui.refreshable
def paper_panel() -> None:
    from dashboard.core import paper
    from dashboard.execution import broker as _bk
    trades = paper.all_trades()
    closed = [t for t in trades if t["status"] != "OPEN"]
    open_t = [t for t in trades if t["status"] == "OPEN"]
    # FIXED 2026-08-19: a CANCELLED/VOID trade never had a real outcome -- its
    # realized_r is always 0.0 by convention (see paper.py's _update_resolution()
    # call sites), not a genuine win or loss. Feeding it into win_rate/expectancy
    # alongside real WIN/LOSS/EXPIRED trades silently deflates both -- confirmed live:
    # ATR rr3.0 showed "win rate 3%" (1/30) when the real figure among trades that
    # actually resolved is 1/8 = 12.5% (still n<30, correctly untrustworthy -- just
    # not the misleading 3%). 20 of those 30 were CWB signals that never got funded
    # within 30min (a real, separate pattern -- see HANDOFF), not losses.
    resolved = [t for t in closed if t["status"] in ("WIN", "LOSS", "EXPIRED")]
    not_resolved = [t for t in closed if t["status"] not in ("WIN", "LOSS", "EXPIRED")]

    _title = "Live Trades — Track Record" if _bk.is_live() else "Paper Trades — Forward Track Record"
    with ui.row().classes("items-center justify-between w-full"):
        ui.label(_title).classes("text-lg font-bold")
        with ui.row().classes("gap-1"):
            ui.button("Export results", icon="download", on_click=_export_results).props("flat dense")
            ui.button("Archive & reset", icon="inventory_2", on_click=_archive_reset).props("flat dense")
            ui.button("View archive", icon="history", on_click=_open_archive).props("flat dense")
    ui.label("Auto-logged from qualifying signals (both SL/TP methods). "
             "Expectancy in R is the number that matters, not win rate. "
             "Times shown in HKT (UTC+8).")\
        .classes("text-xs text-grey-6")

    # stats grouped by method -- resolved (WIN/LOSS/EXPIRED) trades only, see fix note above
    methods = sorted({t["method"] for t in resolved})
    with ui.row().classes("w-full flex-wrap gap-3"):
        if not resolved:
            # FIXED 2026-07-23: this said "5-day horizon" -- wrong on two counts.
            # HORIZON_DAYS=5 is a BAR count (weekly bars), not calendar days; the actual
            # calendar-day figure is HORIZON_CAL=35 (5 weekly bars), already referenced
            # correctly elsewhere in the codebase (e.g. the pending-card ETA text). This one
            # line never got updated and understated how long a trade can sit unresolved by 7x.
            ui.label("No resolved trades yet. They settle as price hits SL/TP or the "
                     f"{paper.HORIZON_CAL}-day horizon passes.").classes("text-sm text-grey")
        for m in methods:
            rs = [t["realized_r"] for t in resolved if t["method"] == m]
            s = paper.stats(rs)
            color = "bg-green-1" if s["expectancy_R"] > 0 else "bg-red-1"
            with ui.card().classes(f"min-w-[230px] {color}"):
                ui.label(m).classes("font-bold")
                ui.label(f"expectancy: {s['expectancy_R']:+.3f} R").classes("text-base font-bold")
                ui.label(f"win rate: {s['win_rate']:.0%}   n={s['n']}").classes("text-sm")
                pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
                ui.label(f"PF {pf}   total {s['total_R']:+.1f}R").classes("text-xs text-grey-7")
                if not s["trustworthy"]:
                    ui.label("n<30 — too few to trust").classes("text-xs text-orange italic")

    # ADDED 2026-07-18: "funded" column on both tables below -- a signal that never got a
    # broker order (portfolio cap held it back, etc.) still gets tracked and resolved against
    # real price action for signal-quality evaluation (see paper.resolve_open()'s
    # executed_ids-aware exit_reason), but a bare "LOSS  R -1.00" row looked identical whether
    # real money was ever on the line or not -- confirmed a real user had to ask why a CWB
    # "loss" happened, since nothing in this table said it was never funded. broker.executed_ids()
    # is a local SQLite query, not a broker round-trip, so this is cheap to check per render.
    _executed = _bk.executed_ids() if _bk.is_ib() else set()

    # P1 spec: Trades sub-filter (All / Active funded / Pending signal-only / Closed /
    # Cancelled) + search
    _trades_filter = SETTINGS.get("trades_filter", "all")
    _trades_search = SETTINGS.get("trades_search", "")
    def _set_trades_filter(e) -> None:
        SETTINGS.update(trades_filter=e.value); _save_settings(); paper_panel.refresh()
    def _set_trades_search(e) -> None:
        SETTINGS.update(trades_search=e.value or ""); _save_settings(); paper_panel.refresh()
    with ui.row().classes("items-center gap-2 w-full flex-wrap mt-2"):
        ui.toggle({"all": "All", "active": "Active ✓", "pending": "Pending ○",
                   "closed": "Closed", "cancelled": "Cancelled"},
                  value=_trades_filter, on_change=_set_trades_filter).props("dense")
        ui.input(placeholder="Filter instrument…", value=_trades_search,
                 on_change=_set_trades_search).props("dense clearable").classes("w-[200px]")
        ui.label(f"Total {len(trades)} · Open {len(open_t)} · Closed {len(closed)}")\
            .classes("text-xs text-grey-6 ml-auto")

    # open trades (selectable -> archive specific records)
    # COLUMN ORDER 2026-08-19, user-requested: "dir" demoted near the end -- this
    # strategy is long-only (BROKER=ib), so per-row direction is never a decision-
    # relevant field; ordered by what a glance actually needs (what/is-it-funded/risk
    # params) before administrative detail (method/opened/dir/id).
    _show_open = _trades_filter in ("all", "active", "pending")
    _show_closed = _trades_filter in ("all", "closed")
    _show_cancelled = _trades_filter in ("all", "cancelled")
    if _show_open and open_t:
        # apply active/pending filter + search
        _open_filtered = [t for t in open_t if (
            (_trades_filter != "active" or t["id"] in _executed) and
            (_trades_filter != "pending" or t["id"] not in _executed) and
            (not _trades_search or _trades_search.lower() in t["instrument"].lower()))]
        if _open_filtered:
            with ui.row().classes("items-center gap-2 mt-2"):
                ui.label(f"Open ({len(_open_filtered)}/{len(open_t)})").classes("text-sm font-bold")
                ui.button("Archive selected", icon="archive",
                          on_click=lambda: _archive_records(open_tbl)).props("flat dense")
            rows = [{"instrument": t["instrument"], "funded": "✓ broker" if t["id"] in _executed else "○ signal only",
                     "entry": round(t["entry"], 4), "SL": round(t["sl"], 4), "TP": round(t["tp"], 4),
                     "R:R": t["rr"], "method": t["method"], "opened": _fmt_ts(t["ts"]),
                     "dir": t["direction"], "id": t["id"]} for t in _open_filtered]
            col_order = ["instrument", "funded", "entry", "SL", "TP", "R:R", "method", "opened", "dir", "id"]
            with ui.element("div").classes("w-full overflow-x-auto rounded border"):
                open_tbl = ui.table(rows=rows, row_key="id", selection="multiple",
                                    columns=_sortable_cols(col_order),
                                    pagination={"sortBy": "opened", "descending": True, "rowsPerPage": 10})\
                    .classes("w-full min-w-[680px]").props("dense flat")
        elif _trades_search or _trades_filter != "all":
            ui.label("No open trades match filters.").classes("text-sm text-grey")
    # recent closed (selectable -> archive specific records) -- resolved (WIN/LOSS/
    # EXPIRED) only; CANCELLED/VOID moved to a collapsed section below.
    if _show_closed and resolved:
        _resolved_filtered = [t for t in resolved
                              if not _trades_search or _trades_search.lower() in t["instrument"].lower()]
        if _resolved_filtered:
            with ui.row().classes("items-center gap-2 mt-2"):
                ui.label(f"Recent closed ({len(_resolved_filtered)}/{len(resolved)})").classes("text-sm font-bold")
                ui.button("Archive selected", icon="archive",
                          on_click=lambda: _archive_records(closed_tbl)).props("flat dense")
        # ADDED 2026-07-24: entry/SL/TP/exit + a $ P&L column -- R alone doesn't say how much
        # a closed trade actually won or lost. risk_money (the real $ risked at execution,
        # same source _monthly_attribution() uses, always USD regardless of account base ccy)
        # is only ever recorded for broker-funded trades -- '○ signal only' rows show "—"
        # rather than a fabricated dollar figure, since no real money was ever on the line.
        # ADDED 2026-08-19: qty alongside risk_money -- "invested (USD)" = qty x entry,
        # the actual notional committed (distinct from risk_money, which is only the
        # $ AT RISK to the stop, not the full position size).
        with paper._LOCK, paper._conn() as c:
            mirror_rows = c.execute(
                f"SELECT paper_id, risk_money, qty FROM {_bk.mirror_table()}").fetchall()
        risk_by_id = {r[0]: r[1] for r in mirror_rows}
        qty_by_id = {r[0]: r[2] for r in mirror_rows}
        # cumulative R: chronological (oldest -> newest) running total across the FULL
        # resolved history, not just the visible slice below -- matches how the equity
        # curve/retrospective tab already compute it. Attached per-row as a fixed value,
        # independent of whatever sort order the table is currently displayed in.
        # FIXED 2026-08-19: was summing ALL resolved trades regardless of funding --
        # confirmed live this disagreed with the Retro tab's own cumulative R (-4.03 here
        # vs -0.016 there) because 4 of the 8 resolved trades were "○ signal only" (never
        # funded at the broker, a real price-action outcome but no real money on it) --
        # Retro's equity_curve() only ever counts broker-EXECUTED trades (see its own
        # header text: "signals never placed are excluded"), and this table's OWN
        # neighboring "P&L (USD)" column already makes the same distinction (shows "—"
        # for signal-only rows). Restricting to the same _executed set makes both figures
        # agree and keeps cumulative R meaning the same thing as the P&L column next to
        # it -- a signal-only row's cumulative R now correctly falls through to "—" via
        # cum_r_by_id.get()'s default below, same as its P&L.
        cum_r_by_id: dict[int, float] = {}
        running = 0.0
        for t in sorted(resolved, key=lambda t: t["exit_ts"] or ""):
            if t["id"] not in _executed:
                continue
            running += t["realized_r"]
            # 3dp, matching equity_curve()'s own precision -- 2dp here made a
            # mathematically-identical number LOOK different from Retro's (-0.02 vs
            # -0.016), confusing on top of the population mismatch this same fix addresses.
            cum_r_by_id[t["id"]] = round(running, 3)

        def _closed_row(t: dict) -> dict:
            # FIXED 2026-08-25: gated on _executed (same broker-truth set the neighboring
            # "funded" column already uses), not merely "does an ib_mirror row exist" -- a
            # row can exist for an order that was cancelled while STILL UNFILLED (see
            # ib_exec.py's 2026-08-25 note), meaning NOTHING was ever actually bought. That
            # row's stale qty/risk_money (recorded at order-placement time, never zeroed)
            # used to show a fabricated real-looking "invested"/"P&L (USD)" here -- confirmed
            # live: paper #140 showed "invested $257, P&L +$270" for a position that was
            # never actually funded at all.
            invested = (f"{qty_by_id[t['id']] * t['entry']:,.0f}"
                       if t["id"] in qty_by_id and t["id"] in _executed else "—")
            pnl = (f"{t['realized_r'] * risk_by_id[t['id']]:+,.0f}"
                  if t["id"] in risk_by_id and t["id"] in _executed else "—")
            return {"instrument": t["instrument"], "status": t["status"],
                   "R": round(t["realized_r"], 2), "cumulative R": cum_r_by_id.get(t["id"], "—"),
                   "invested (USD)": invested, "P&L (USD)": pnl,
                   "closed": _fmt_ts(t["exit_ts"]), "opened": _fmt_ts(t["ts"]),
                   "funded": "✓ broker" if t["id"] in _executed else "○ signal only",
                   "exit": round(t["exit_price"], 4) if t["exit_price"] else None,
                   "entry": round(t["entry"], 4), "SL": round(t["sl"], 4), "TP": round(t["tp"], 4),
                   "method": t["method"], "dir": t["direction"], "id": t["id"]}

        col_order = ["instrument", "status", "R", "cumulative R", "invested (USD)", "P&L (USD)",
                    "closed", "opened", "funded", "exit", "entry", "SL", "TP", "method", "dir", "id"]
        if _resolved_filtered:
            rows = [_closed_row(t) for t in _resolved_filtered[:20]]
            with ui.element("div").classes("w-full overflow-x-auto rounded border"):
                closed_tbl = ui.table(rows=rows, row_key="id", selection="multiple",
                                      columns=_sortable_cols(col_order),
                                      pagination={"sortBy": "closed", "descending": True, "rowsPerPage": 10})\
                    .classes("w-full min-w-[900px]").props("dense flat")\
                    .tooltip("'R' is what the signal-logic scored regardless of funding -- "
                             "'P&L (USD)' is the real $ risked x R, only available for '✓ broker' "
                             "rows -- '○ signal only' rows never had a real broker order, see the "
                             "Retrospective tab for broker-executed-only KPIs")
                # ADDED 2026-08-19, user-requested: demote '○ signal only' rows visually (tinted
                # row, muted text, thin left rule) instead of every row reading the same weight --
                # a real broker outcome and a hypothetical never-funded one looked identical before,
                # requiring a column scan to tell them apart. Standard Quasar body-slot override
                # (generic over props.cols, not hand-listing every column) since QTable has no
                # built-in per-row conditional class.
                # FIXED 2026-08-19: this override replaced the WHOLE row template but omitted the
                # selection checkbox cell Quasar's own default body slot adds for selection="multiple"
                # -- confirmed live: 17 <th> (16 data + checkbox) vs only 16 <td> per row, so every
                # column rendered one position early (TP showed the method value, SL showed TP's,
                # etc. -- user caught it as "TP shows ATR rr3.0"). Quasar's documented pattern for a
                # custom body slot alongside selection is an explicit checkbox <q-td> first.
                closed_tbl.add_slot("body", '''
                    <q-tr :props="props"
                          :class="props.row.funded.includes('signal') ? 'bg-grey-2' : ''">
                        <q-td auto-width>
                            <q-checkbox v-model="props.selected" />
                        </q-td>
                        <q-td v-for="col in props.cols" :key="col.name" :props="props"
                              :class="props.row.funded.includes('signal') ?
                                  (col.name === 'instrument' ? 'text-grey-7' :
                                   col.name === 'funded' ? 'text-grey-6 text-italic' : 'text-grey-6')
                                  : ''"
                              :style="props.row.funded.includes('signal') && col.name === 'instrument' ?
                                  'border-left: 2px solid #bdbdbd' : ''">
                            {{ col.name === 'status' && props.row.funded.includes('signal') ?
                               col.value.toLowerCase() : col.value }}
                        </q-td>
                    </q-tr>
                ''')
        else:
            ui.label("No closed trades match filters.").classes("text-sm text-grey mt-2")
            # keep variable defined for outer scope (archive logic checks closed_tbl)
            closed_tbl = None  # type: ignore

        if not_resolved and _show_cancelled:
            # filtered cancelled too
            _cancelled_filtered = [t for t in not_resolved
                                   if not _trades_search or _trades_search.lower() in t["instrument"].lower()]
            if _cancelled_filtered:
                with ui.expansion(f"{len(_cancelled_filtered)}/{len(not_resolved)} cancelled — never had a real "
                                  "outcome, show").classes("w-full text-sm text-grey-6"):
                    rows = [_closed_row(t) for t in _cancelled_filtered[:20]]
                    with ui.element("div").classes("w-full overflow-x-auto rounded border"):
                        ui.table(rows=rows, row_key="id",
                                columns=_sortable_cols(col_order),
                                pagination={"sortBy": "closed", "descending": True, "rowsPerPage": 0})\
                            .classes("w-full min-w-[900px]").props("dense flat")



def _monthly_attribution() -> list[dict]:
    """Monthly $ breakdown: trend-strategy / sleeve / other. Trend and sleeve are computed
    from CLOSED trades' realized_r * risk_money (risk_money is the ACTUAL dollar risk sized
    at execution time, read from ib_mirror/mt5_mirror -- not re-derived, so it's exact even
    if RISK_PER_TRADE changed between trades). 'Other' is a deliberate RESIDUAL against the
    deposit-adjusted equity curve (total month-over-month change minus trend minus sleeve),
    not a separately-modeled cash-interest number -- there's no historical AccruedCash time
    series stored anywhere to compute that directly, so labeling the gap 'other' is the
    honest choice over fabricating a precise-looking cash figure. Whole table in USD (trend/
    sleeve $ are natively USD from risk sizing; the equity curve is converted from the
    account's base currency via the same HKD peg used elsewhere)."""
    from dashboard.core import paper, store, sleeve
    from dashboard.execution import broker as _bk
    from dashboard.data import ib_client
    trades = paper.all_trades()
    closed = [t for t in trades if t["status"] != "OPEN" and t.get("exit_ts")]
    with paper._LOCK, paper._conn() as c:
        mirror_rows = c.execute(f"SELECT paper_id, risk_money FROM {_bk.mirror_table()}").fetchall()
    risk_by_id = dict(mirror_rows)
    # FIXED 2026-08-25: same gate as paper_panel()'s _closed_row() -- an ib_mirror row can
    # exist for an order cancelled while still UNFILLED (nothing ever actually bought), whose
    # stale risk_money (from order-placement time) would otherwise silently pollute this $
    # attribution. See ib_exec.py's 2026-08-25 note.
    _executed = _bk.executed_ids() if _bk.is_ib() else set()

    buckets: dict[str, dict] = {}
    for t in closed:
        if t["id"] not in _executed:
            continue
        risk_money = risk_by_id.get(t["id"])
        if risk_money is None:
            continue
        month = t["exit_ts"][:7]
        b = buckets.setdefault(month, {"trend": 0.0, "sleeve": 0.0})
        dollar_pnl = t["realized_r"] * risk_money
        if t["method"] == sleeve.SLEEVE_METHOD:
            b["sleeve"] += dollar_pnl
        else:
            b["trend"] += dollar_pnl

    hist, _ts = store.cache_get("equity_history")
    flows, _fts = store.cache_get("cash_flows")
    hist = paper.with_inception(hist or [])
    if not hist:
        return []
    ccy = hist[0][2] if len(hist[0]) > 2 else "USD"
    usd_per_ccy = ib_client._PEG_USD_PER.get(ccy, 1.0)
    adj = paper.deposit_adjusted_series(hist, flows)
    month_end_usd: dict[str, float] = {}
    for (ts, *_), av in zip(hist, adj):
        m = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(HKT).strftime("%Y-%m")
        month_end_usd[m] = av * usd_per_ccy   # last write per month wins (hist is ascending)

    months = sorted(set(list(buckets.keys()) + list(month_end_usd.keys())))
    out, prev_val = [], None
    for m in months:
        b = buckets.get(m, {"trend": 0.0, "sleeve": 0.0})
        cur_val = month_end_usd.get(m)
        total = (cur_val - prev_val) if (cur_val is not None and prev_val is not None) else None
        other = (total - b["trend"] - b["sleeve"]) if total is not None else None
        out.append({"month": m, "trend": b["trend"], "sleeve": b["sleeve"],
                    "total": total, "other": other})
        if cur_val is not None:
            prev_val = cur_val
    return out


@ui.refreshable
def portfolio_panel() -> None:
    """IBKR portfolio overview in the account base currency (HKD): total value,
    overall P&L (realized + unrealized), equity line chart, and allocation pie."""
    from dashboard.core import paper, store
    from dashboard.data import ib_client
    from dashboard.execution import broker as _bk
    if not _bk.is_ib():
        return
    acct = service.STATE.get("account") or {}
    positions = service.STATE.get("positions") or {}
    # fall back to the last persisted snapshot if the live read is momentarily empty
    if acct.get("NetLiquidation") is None:
        snap, _snts = store.cache_get("portfolio_snapshot")
        if snap and (snap.get("account") or {}).get("NetLiquidation") is not None:
            acct = snap["account"]
            positions = {int(k): v for k, v in (snap.get("positions") or {}).items()}
    ccy = acct.get("_ccy", "")
    nl = acct.get("NetLiquidation")
    cash = acct.get("TotalCashValue")
    gpv = acct.get("GrossPositionValue")
    if nl is None:
        ui.label("Portfolio").classes("text-lg font-bold")
        ui.label("IBKR account data not loaded yet — connecting to gateway…")\
            .classes("text-sm text-grey")
        return
    usd_to_base = 1.0 / ib_client._PEG_USD_PER.get(ccy, 1.0)   # USD position vals -> base ccy
    upnl = sum(p.get("profit", 0.0) for p in positions.values()) * usd_to_base
    hist, _ts = store.cache_get("equity_history")
    hist = paper.with_inception(hist or [])
    base0 = hist[0][1] if hist else nl                        # value when tracking started
    base0_ts = hist[0][0] if hist else 0
    flows, _fts = store.cache_get("cash_flows")
    # net deposits/withdrawals since tracking began -- these move NetLiquidation but are NOT
    # trading P&L, so they must be excluded (see service.py's equity_history cash-flow logging)
    net_flows = sum(f[1] for f in (flows or []) if f[0] >= base0_ts)
    total_pl = nl - base0 - net_flows
    # BUG FIXED 2026-07-10: pct used to divide by base0 alone -- fine when tracking starts
    # AFTER the account is funded, but wrong once a deposit lands on top of a tiny/near-zero
    # starting snapshot (confirmed live: base0=HKD 40 from before the account's real HKD
    # 10,000 deposit, so a genuine -HKD 31 cost showed as -78% instead of the true ~-0.3%).
    # The capital base P&L should be measured against is base0 PLUS everything deposited
    # since, not the original snapshot alone -- same denominator the numerator already
    # implicitly uses (total_pl nets deposits OUT of the delta; pct must net them INTO the base).
    capital_base = base0 + net_flows
    pct = (total_pl / capital_base * 100.0) if capital_base else 0.0

    def _money(x):
        return f"{ccy} {x:,.0f}"

    def _stat(label, value, color="text-grey-9", tip=""):
        with ui.column().classes("items-start gap-0"):
            ui.label(label).classes("text-xs text-grey-6 uppercase")
            lbl = ui.label(value).classes(f"text-xl font-bold {color}")
            if tip:
                lbl.tooltip(tip)

    sweep = service.STATE.get("cash_sweep") or {}
    sgov_base = float(sweep.get("sgov_value_base", 0.0)) if sweep.get("enabled") else 0.0
    _tb = service.STATE.get("tbill_rate")               # live ^IRX (13wk T-bill), %
    sgov_rate = (_tb - 0.07) if _tb else None           # SGOV ≈ ^IRX minus 0.07% fee
    ib_rate = max(_tb - 0.55, 0.0) if _tb else None     # IB pays ~benchmark-0.5% (3.12% @ IRX 3.67)
    sgov_yld = f"~{sgov_rate:.1f}%" if sgov_rate else "~T-bill rate"
    invested = (gpv - sgov_base) if gpv is not None else None   # strategy deployment ex-SGOV

    with ui.row().classes("items-baseline gap-3"):
        ui.label("Portfolio").classes("text-lg font-bold")
        # B3 2026-08-26: standardized freshness badge (grey/amber/red by age) replaces the
        # old ad-hoc always-grey "updated X ago" label.
        _lc = service.STATE.get("last_cheap")
        if _lc is not None:
            _freshness_label(_lc, warn_min=30, bad_min=90).tooltip(
                "age of the last cheap-layer (price/position) refresh; amber = stale, "
                "red = the tick loop may be stuck")
        elif service.STATE.get("portfolio_ts"):
            _t = dt.datetime.fromtimestamp(service.STATE["portfolio_ts"], tz=dt.timezone.utc).astimezone(HKT)
            ui.label(f"last refreshed {_t.strftime('%m-%d %H:%M')} HKT · refreshing…")\
                .classes("text-xs text-orange")

    # HEADLINE: the one question everything else on this panel supports -- are you up or
    # down overall. Made deliberately bigger/colored/its-own-card so it can't be mistaken
    # for just one stat among many -- the cash/financing figures below look similar in
    # shape (a label + a number) but answer a DIFFERENT question (how positions are
    # funded) and were getting misread as profit/loss (a negative cash buffer is normal
    # margin financing, not a loss -- see its tooltip below).
    with ui.card().classes(("bg-green-1" if total_pl >= 0 else "bg-red-1") + " w-full"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("trending_up" if total_pl >= 0 else "trending_down",
                    color="green" if total_pl >= 0 else "red").classes("text-2xl")
            ui.label("You are " + ("up" if total_pl >= 0 else "down")).classes(
                "text-sm text-grey-7")
        ui.label(f"{_money(total_pl)}  ({pct:+.2f}%)").classes(
            "text-3xl font-bold " + ("text-green" if total_pl >= 0 else "text-red"))
        ui.label("Total trading P&L since tracking began — excludes deposits/withdrawals, "
                 "includes both open and closed trades").classes("text-xs text-grey-6")
        _spy = service.STATE.get("spy_benchmark")
        if _spy and _spy.get("base_px"):
            spy_pct = (_spy["cur_px"] / _spy["base_px"] - 1.0) * 100.0
            excess = pct - spy_pct
            with ui.row().classes("items-center gap-2 mt-1"):
                ui.label(f"vs SPY {spy_pct:+.2f}%").classes("text-xs text-grey-7")
                ui.label(f"excess {excess:+.2f}%").classes(
                    "text-xs font-bold " + ("text-green" if excess >= 0 else "text-red"))\
                    .tooltip("Your % return vs. buy-and-hold SPY over the SAME tracking "
                             "window — the honest 'is this strategy earning its keep' check. "
                             "SPY return is unweighted/undiversified for comparison purposes "
                             "only, not a claim the account should hold 100% SPY.")

    with ui.row().classes("w-full flex-wrap gap-6 items-stretch mt-2"):
        _stat("Total value", _money(nl), "text-grey-9",
              f"Net liquidation value of the {_bk.name()} account")
        # "Unrealized (open)" and "Invested" are only meaningful once there's something
        # actually open -- showing two redundant "HKD 0" stats when the account is fully in
        # cash was just clutter (user feedback 2026-07-10). Gate both on real GPV.
        if gpv is not None and gpv > 0:
            _stat("Unrealized (open)", _money(upnl),
                  "text-green" if upnl >= 0 else "text-red",
                  "P&L of currently open positions (USD converted at the HKD peg)")
            if invested is not None:
                _stat("Invested", _money(invested), "text-grey-9",
                      "Market value of strategy ETF positions (excludes SGOV cash parking)")
        else:
            ui.label("Fully in cash — no open positions").classes(
                "text-sm text-grey-6 self-center")

    # 2026-07-23: shortened from an always-visible full sentence -- the clarifying detail
    # ("NOT profit or loss, see the P&L card above") moved into a tooltip on a small info
    # icon instead, same pattern as elsewhere on this pass.
    with ui.row().classes("items-center gap-1 mt-3"):
        ui.label("Cash & financing").classes("text-xs text-grey-6 uppercase")
        ui.icon("info", size="xs").classes("text-grey-5").tooltip(
            "How positions are funded -- NOT profit or loss (see the P&L card above for that)")
    # Two intentional rows: line 1 groups Cash (buffer) with what it's ACTUALLY COSTING OR
    # EARNING (Interest accrued + Projected interest) so the causality is obvious at a glance
    # -- a negative buffer directly explains a negative projected interest, and vice versa
    # (user feedback 2026-07-10: these used to be split across two separate rows, which hid
    # that link). Line 2 is the currency/form BREAKDOWN of that cash, plus buying power as
    # the financing-capacity reference.
    MARGIN_DEBIT_RATE = 5.5   # approx IBKR HKD/USD margin rate; not account-specific (no API
                              # field for the live per-account rate) -- see HANDOFF ~5-6% figure
    with ui.row().classes("w-full flex-wrap gap-6 items-stretch"):
        if cash is not None:
            _stat("Cash (buffer)", _money(cash), "text-grey-9",
                  "Un-parked cash kept available for the strategy. Negative just means the "
                  "open positions' combined size is funded partly on margin (normal with "
                  "several concurrent positions) — it is NOT a loss.")
        # 2026-07-24: "Interest accrued" and "Projected interest (1mo)" used to be 2 separate
        # _stat() blocks -- same underlying concept (interest), split in two. Merged into one
        # "Interest" stat, same pattern as the Cash breakdown merge last round.
        accrued = acct.get("AccruedCash")
        _interest_bits = []
        _interest_neg = False
        if accrued is not None:
            _interest_bits.append(f"accrued {_money(accrued)}")
            _interest_neg = _interest_neg or accrued < 0
        # projected interest next month: SGOV @ ^IRX + USD-cash buffer @ IB credit/debit rate.
        # Borrow and lend rates are NOT symmetric -- a positive cash buffer earns the ~benchmark
        # credit rate (ib_rate), but a NEGATIVE buffer is a margin debit charged ~5-6% (see the
        # "USD cash" tooltip below), a materially higher rate. Using ib_rate for both understated
        # the true cost of the (normal, expected) small margin debit that comes from sizing
        # multiple concurrent ETF positions independently -- fixed 2026-07-09.
        sgov_mo = cash_mo = cash_rate = None
        if sgov_rate is not None:
            sgov_mo = sgov_base * sgov_rate / 100.0 / 12.0
            cash_val = cash or 0.0
            cash_rate = (ib_rate or 0.0) if cash_val >= 0 else MARGIN_DEBIT_RATE
            cash_mo = cash_val * cash_rate / 100.0 / 12.0
            proj = sgov_mo + cash_mo
            _interest_bits.append(f"next mo. ~{_money(proj)}")
            _interest_neg = _interest_neg or proj < 0
        if _interest_bits:
            with ui.column().classes("items-start gap-0"):
                ui.label("Interest").classes("text-xs text-grey-6 uppercase")
                _tip = ("IB interest accrued on CASH balances since the last monthly payout "
                        "(running total, resets monthly). NOT from SGOV — SGOV pays separate "
                        "monthly distributions.")
                if sgov_mo is not None:
                    _tip += (f" Next month projected from SGOV {_money(sgov_mo)} @ "
                            f"{sgov_rate:.1f}% + USD cash {_money(cash_mo)} @ {cash_rate:.1f}% "
                            f"({'margin debit rate, approx' if cash_mo < 0 else 'live ^IRX-derived rate'}).")
                ui.label("  ·  ".join(_interest_bits)).classes(
                    "text-xl font-bold " + ("text-red" if _interest_neg else "text-green"))\
                    .tooltip(_tip)

    with ui.row().classes("w-full flex-wrap gap-6 items-stretch mt-2"):
        # 2026-07-23: SGOV/HKD/USD cash used to be 3 separate _stat() blocks, each with its
        # own uppercase mini-heading -- same underlying idea ("where is idle cash currently
        # sitting"), tripled. Merged into one "Cash breakdown" stat; the stuck-conversion
        # warning (a real, actionable alert) now shows inline as a ⚠ marker plus a combined
        # tooltip, instead of only appearing on its own separate USD-cash block.
        fx = service.STATE.get("fx_usd") or {}
        _cash_bits = []
        _stuck = fx.get("stuck", False)
        _neg = False
        if sgov_base > 0:
            _cash_bits.append(f"SGOV {_money(sgov_base)}")
        usd_c = hkd_c = None
        if fx.get("enabled"):
            usd_c = fx.get("usd_cash", 0.0)
            hkd_c = fx.get("hkd_cash", 0.0)
            _neg = hkd_c < 0 or usd_c < 0
            _cash_bits.append(f"HKD {hkd_c:,.0f}")
            _cash_bits.append(f"USD {usd_c:,.0f}" + (" ⚠" if _stuck else ""))
        if _cash_bits:
            with ui.column().classes("items-start gap-0"):
                ui.label("Cash breakdown").classes("text-xs text-grey-6 uppercase")
                _col = "text-red" if (_stuck or _neg) else "text-grey-9"
                _tip = (f"Where idle cash currently sits -- SGOV (0-3mo T-bill ETF, yielding "
                        f"{sgov_yld}, auto-swept), HKD (converts down to a small residual buffer "
                        "each cycle), USD (auto-converts from idle HKD each cycle to earn USD "
                        "yield). NOT profit/loss -- a negative HKD/USD figure just means that "
                        "side is a margin debit (~5-6% interest), same story as Cash (buffer) "
                        "above.")
                if _stuck:
                    _tip += (" ⚠ HKD→USD conversion keeps failing to actually fill (repeated "
                            "attempts, no real USD balance yet) -- most likely the account's "
                            "Forex trading permission isn't enabled/approved.")
                ui.label("  ·  ".join(_cash_bits)).classes(f"text-xl font-bold {_col}")\
                    .tooltip(_tip)
        buying_power = acct.get("BuyingPower")
        if buying_power is not None:
            _stat("Buying power (購買力)", _money(buying_power), "text-grey-9",
                  "Total purchasing capacity IBKR will extend right now (cash + available "
                  "margin). On a MARGIN account this exceeds Total value (e.g. paper: ~5x, "
                  "reflecting the ETF_POS_CAP leverage design); on a CASH-only account it's "
                  "capped near available cash with no multiple. If this stays equal to Total "
                  "value on an account you expect to be margin-enabled, margin capacity likely "
                  "isn't actually active — confirm in IBKR's Account Management portal.")
        # ADDED 2026-08-18 (user-requested): net_flows (computed above for the Total P&L
        # stat's own math) was never itself surfaced anywhere -- a real deposit-heavy account
        # gave the user no on-dashboard way to see "how much have I actually put in" without
        # opening the Cash flows dialog and summing the list by hand. Shown even when zero
        # (unlike Unrealized/Invested's gating above) -- "nothing recorded" is itself
        # informative here, matching the Cash flows dialog's own "Nothing recorded yet" state.
        _stat("Net deposits", _money(net_flows), "text-grey-9",
              "Total money moved into the account minus money moved out, since tracking "
              "began — not trading P&L (see Total P&L above). Open Cash flows to see or "
              "edit the individual entries.")

    # ADDED 2026-08-26: exposure by asset class -- breadth is this strategy's whole thesis
    # ("the book's avg pairwise correlation stays low because the tickers span genuinely
    # different asset classes"), yet nothing on the dashboard showed WHERE the money
    # currently sits by class, or how much PORTFOLIO_CAP headroom remains.
    if positions and nl:
        try:
            _cls_sum: dict[str, float] = {}
            _seen_tk: set = set()
            _sym_of_id = {t["id"]: t["instrument"] for t in paper.open_trades()}
            for _pid in sorted(positions, key=lambda k: k not in _sym_of_id):
                _p = positions[_pid]
                _tk = _p.get("ticket")
                if _tk is not None:
                    if _tk in _seen_tk:
                        continue
                    _seen_tk.add(_tk)
                _mv_usd = _p["volume"] * _p["open"] + _p.get("profit", 0.0)
                if _mv_usd <= 0:
                    continue
                _sym = _sym_of_id.get(_pid) or _p.get("symbol")
                _cls = _asset_class_for(_sym) or "Other"
                _cls_sum[_cls] = _cls_sum.get(_cls, 0.0) + _mv_usd * usd_to_base
            if _cls_sum:
                _gross_pct = sum(_cls_sum.values()) / nl * 100.0
                try:
                    _cap_pct = float(os.environ.get("PORTFOLIO_CAP", "1.0")) * 100.0
                except Exception:                          # noqa: BLE001
                    _cap_pct = None
                _head = (_cap_pct - _gross_pct) if _cap_pct is not None else None
                _head_col = ("text-red font-bold" if _head is not None and _head < 0 else
                             "text-orange-8 font-bold" if _head is not None and _head < 15
                             else "text-grey-9")
                with ui.row().classes("items-baseline gap-3 w-full mt-3 flex-wrap"):
                    ui.label("Exposure by asset class").classes("text-sm font-bold")
                    _head_txt = (f"gross {_gross_pct:.1f}% of NAV · cap {_cap_pct:.0f}% · "
                                 f"headroom {_head:.1f}%" if _cap_pct is not None else
                                 f"gross {_gross_pct:.1f}% of NAV")
                    ui.label(_head_txt).classes(f"text-xs {_head_col}").tooltip(
                        "Filled strategy positions only — PENDING (not-yet-filled) broker "
                        "orders also consume portfolio-cap room but aren't shown here yet; "
                        "the cap itself accounts for both (see README, 2026-07-13 fix). "
                        "Amber/red headroom means new entries may be cap-blocked soon.")
                _classes_sorted = sorted(_cls_sum.items(), key=lambda kv: -kv[1])
                _palette = ["#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de",
                            "#3ba272", "#fc8452", "#9a60b4", "#ea7ccc"]
                ui.echart({
                    "tooltip": {"trigger": "axis",
                                "formatter": "{b}: {c}%"},
                    "grid": {"left": 90, "right": 40, "top": 5, "bottom": 25},
                    "xAxis": {"type": "value", "max": 100,
                              "axisLabel": {"formatter": "{value}%"}},
                    "yAxis": {"type": "category",
                              "data": [f"{k} ({v / nl * 100:.1f}%)"
                                       for k, v in reversed(_classes_sorted)]},
                    "series": [{"type": "bar", "barWidth": 14,
                                "data": [{"value": round(v / nl * 100, 2),
                                          "itemStyle": {"color": _palette[i % len(_palette)]}}
                                         for i, (_, v) in enumerate(reversed(_classes_sorted))]}],
                }).classes("w-full").style(
                    f"height: {min(44 + 28 * len(_classes_sorted), 260)}px")
        except Exception as e:                             # noqa: BLE001 -- never break the panel
            from dashboard.core.log import log
            log.debug("exposure-by-class render failed: %s", e)

    # Period control: governs BOTH charts below. The drawdown "now" badge + the peak-tracking
    # always use the FULL history (correctness -- a window can't hide the true current DD from
    # the all-time peak); the period only trims which POINTS are plotted, for readability.
    def _set_chart_period(e) -> None:
        SETTINGS["chart_period"] = e.value
        _save_settings()
        portfolio_panel.refresh()
    with ui.row().classes("items-center gap-2 mt-2"):
        ui.label("Period:").classes("text-xs text-grey-6")
        ui.toggle(list(CHART_PERIODS), value=SETTINGS["chart_period"], on_change=_set_chart_period)\
            .props("dense").tooltip("window shown in the charts below (both value & drawdown)")

    _lookback_days = CHART_PERIODS.get(SETTINGS["chart_period"])
    _cutoff = (hist[-1][0] - _lookback_days * 86400) if (_lookback_days and hist) else None
    _adj_full = paper.deposit_adjusted_series(hist, flows)  # pure trading P&L, deposits/withdrawals netted out

    # equity line chart (account value over time, base ccy)
    def _set_chart_scale(e) -> None:
        SETTINGS["chart_scale"] = e.value
        _save_settings()
        portfolio_panel.refresh()

    def _set_chart_view(e) -> None:
        SETTINGS["chart_view"] = e.value
        _save_settings()
        portfolio_panel.refresh()
    with ui.row().classes("items-center justify-between w-full mt-2"):
        # FIXED 2026-07-29: this heading said "Account value over time" unconditionally, but
        # the chart itself switches between P&L (ex-deposits) and raw Account value based on
        # the View toggle right below it -- and P&L is the DEFAULT (SETTINGS["chart_view"]),
        # so most views showed a P&L line under an "Account value" label.
        _chart_title = ("P&L over time (ex-deposits)" if SETTINGS["chart_view"] == "P&L (ex-deposits)"
                        else "Account value over time")
        ui.label(f"{_chart_title} ({ccy})").classes("text-sm font-bold")
        # 2026-07-24: View + Scale used to sit inline as 2 more labeled toggle groups right
        # next to Period (3 labeled controls crowding one chart heading). Period is the one
        # actually changed often; View/Scale are rarer, more advanced choices -- moved behind
        # a small gear-icon menu, Period stays inline where it's reached for.
        with ui.button(icon="tune").props("flat dense round size=sm")\
                .tooltip("chart display options (view, scale)"):
            with ui.menu():
                with ui.column().classes("p-3 gap-2"):
                    ui.label("View").classes("text-xs text-grey-6 uppercase")
                    ui.toggle(["P&L (ex-deposits)", "Account value"], value=SETTINGS["chart_view"],
                             on_change=_set_chart_view).props("dense")\
                        .tooltip("P&L (ex-deposits) nets out deposits/withdrawals so the line "
                                 "reads as pure trading performance; Account value shows the "
                                 "raw balance (deposits appear as jumps)")
                    ui.label("Scale").classes("text-xs text-grey-6 uppercase mt-1")
                    ui.toggle(["Truncated", "Zero-baseline"], value=SETTINGS["chart_scale"],
                             on_change=_set_chart_scale).props("dense")\
                        .tooltip("Truncated = zoomed to the data range (shows fine detail); "
                                 "Zero-baseline = y-axis starts at 0 (shows true relative scale)")
    _win_idx = [i for i, h in enumerate(hist) if _cutoff is None or h[0] >= _cutoff]
    _whist = [hist[i] for i in _win_idx]
    if len(hist) >= 2:
        xs = [dt.datetime.fromtimestamp(h[0], tz=dt.timezone.utc).astimezone(HKT).strftime("%m-%d %H:%M") + " HKT" for h in _whist]
        _use_adj = SETTINGS["chart_view"] == "P&L (ex-deposits)"
        # P&L view must be ZERO-referenced (matches the Total P&L stat's own math: nl - base0 -
        # flows) -- _adj_full alone only nets out cash flows, leaving the series sitting at the
        # ORIGINAL starting value (itself a deposit, not profit) instead of 0. Subtract it here;
        # _adj_full stays value-based (unsubtracted) for the drawdown monitor below, where you
        # divide by the peak VALUE, not peak P&L.
        ys = ([_adj_full[i] - hist[0][1] for i in _win_idx] if _use_adj
              else [hist[i][1] for i in _win_idx])
        _zero_base = SETTINGS["chart_scale"] == "Zero-baseline"
        _marks = []
        for fts, famt, fccy in (flows or []):
            if _cutoff is not None and fts < _cutoff:
                continue
            idx = min(range(len(_whist)), key=lambda i: abs(_whist[i][0] - fts), default=None)
            if idx is None:
                continue
            kind = "deposit" if famt > 0 else "withdrawal"
            _marks.append({"xAxis": xs[idx],
                           "label": {"formatter": f"{kind} {famt:+,.0f}", "fontSize": 9},
                           "lineStyle": {"color": "#6b7280", "type": "dotted"}})
        # ADDED 2026-08-26: SPY benchmark OVERLAY -- the headline card compares returns vs
        # SPY as a single % pair, but shapes matter ("did we diverge in the last month or
        # gradually all year?"). When viewing P&L (ex-deposits), overlay a weekly-sampled,
        # % -normalized SPY line over the identical window on a second y-axis. Silently
        # omitted until service.refresh_cheap() has cached spy_series (first fetch happens
        # on the same ~4h cadence as the two-point benchmark).
        _spy_pts = None
        if _use_adj:
            try:
                _spy_raw, _ = store.cache_get("spy_series")
                if _spy_raw and len(_spy_raw) >= 2:
                    _aligned: list[float] = []
                    _j, _base = 0, None
                    for _h in _whist:
                        while _j + 1 < len(_spy_raw) and _spy_raw[_j + 1][0] <= _h[0]:
                            _j += 1
                        if _spy_raw[_j][0] > _h[0]:
                            continue                       # SPY history starts mid-window
                        if _base is None:
                            _base = _spy_raw[_j][1]
                        if _base:
                            _aligned.append(round((_spy_raw[_j][1] / _base - 1) * 100, 2))
                    if len(_aligned) >= 2:
                        _spy_pts = _aligned
            except Exception as e:                         # noqa: BLE001 -- overlay is optional
                from dashboard.core.log import log
                log.debug("SPY overlay unavailable: %s", e)
        _yaxis = {"type": "value", "name": ccy}
        if (_zero_base and not _use_adj):                  # P&L can go negative -- never clip at 0
            _yaxis["min"] = 0
        else:
            _yaxis["scale"] = True
        _series = [{"type": "line", "data": ys, "smooth": True, "areaStyle": {},
                    "lineStyle": {"width": 2},
                    "itemStyle": {"color": "#16a34a" if total_pl >= 0 else "#dc2626"},
                    "markLine": ({"silent": True, "symbol": "none", "data": _marks}
                                 if _marks else None)}]
        if _spy_pts is not None:
            _series.append({"type": "line", "data": _spy_pts, "yAxisIndex": 1,
                            "smooth": True, "symbol": "none",
                            "lineStyle": {"width": 1.5, "type": "dashed", "color": "#9ca3af"},
                            "itemStyle": {"color": "#9ca3af"}})
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "legend": ({"data": [ccy, "SPY %"], "bottom": 0, "textStyle": {"fontSize": 10}}
                       if _spy_pts is not None else None),
            "xAxis": {"type": "category", "data": xs, "boundaryGap": False},
            "yAxis": [_yaxis, *( [{"type": "value", "name": "SPY %", "scale": True,
                                   "splitLine": {"show": False}}]
                                 if _spy_pts is not None else [] )],
            "series": _series,
            "grid": {"left": 75, "right": (_spy_pts is not None and 55 or 20), "top": 20,
                     "bottom": (_spy_pts is not None and 65 or 45)},
        }).classes("w-full h-56").tooltip(
            "P&L (ex-deposits) nets out logged cash flows so this is pure trading performance; "
            "switch to Account value to see the raw balance, with deposits marked as dotted lines."
            if _use_adj else
            "Raw net liquidation value over time -- includes deposits/withdrawals as jumps "
            "(marked with dotted lines). Switch to P&L (ex-deposits) for pure trading performance.")
    else:
        ui.label("Builds as snapshots accrue (~one point / 10 min).")\
            .classes("text-sm text-grey mt-1")

    # DRAWDOWN MONITOR — current % below the running peak (watch the BACKTEST_MAX_DD_PCT line)
    # Uses the DEPOSIT-ADJUSTED series unconditionally (not tied to the chart_view toggle
    # above): a deposit must never look like a new all-time high that resets the peak and
    # hides a real, ongoing trading drawdown -- this has to be correct regardless of what
    # the user happens to have the equity chart's view set to.
    if len(hist) >= 2:
        # FIXED 2026-07-13: this duplicated paper.current_drawdown_pct()'s peak-tracking
        # logic instead of calling it, and drifted out of sync as a result -- it got the
        # 2026-07-11 materiality floor added by hand here, but when a SEPARATE, LATER fix
        # (the 2026-07-18 denominator fix: divide by the real raw equity at the peak, not
        # the tiny deposit-adjusted P&L-only peak value) landed in paper.py, this duplicate
        # never got it. Confirmed live 2026-07-23: this block displayed -7.82% while
        # paper.current_drawdown_pct() (used by the real DD_HALT_PCT gate) correctly said
        # -0.18% for the SAME account at the SAME moment -- the exact bug class the
        # 2026-07-13 fix was meant to prevent, just re-introduced by the duplication itself
        # rather than by a missing floor this time. FIXED PROPERLY this time: call the one
        # shared implementation (paper.drawdown_series(), extracted alongside this fix) so
        # there is no second copy left to fall out of sync again.
        dd_full = paper.drawdown_series(hist, flows)
        cur_dd = dd_full[-1] if dd_full else 0.0
        dxs, dys = [], []
        for i, h in enumerate(hist):          # ALWAYS the full series -- true peak, never windowed
            if _cutoff is None or h[0] >= _cutoff:
                dxs.append(dt.datetime.fromtimestamp(h[0], tz=dt.timezone.utc).astimezone(HKT).strftime("%m-%d %H:%M") + " HKT")
                dys.append(round(dd_full[i], 2))
        ddcol = ("#16a34a" if cur_dd > -5 else
                 "#d97706" if cur_dd > BACKTEST_MAX_DD_PCT else "#dc2626")
        with ui.row().classes("items-baseline gap-2 mt-2"):
            ui.label("Drawdown from peak").classes("text-sm font-bold")
            ui.label(f"now {cur_dd:+.1f}%").classes(
                "text-sm font-bold " + ("text-green" if cur_dd > -5
                                        else "text-orange" if cur_dd > BACKTEST_MAX_DD_PCT
                                        else "text-red"))\
                .tooltip("Always the TRUE current drawdown from the all-time peak, "
                         "regardless of the period selected above -- deposit-adjusted, so a "
                         "cash-in never masquerades as a new peak")
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": dxs, "boundaryGap": False},
            "yAxis": {"type": "value", "name": "% from peak", "max": 0, "scale": True},
            "series": [{"type": "line", "data": dys, "smooth": True, "areaStyle": {},
                        "lineStyle": {"width": 2}, "itemStyle": {"color": ddcol},
                        "markLine": {"silent": True, "symbol": "none", "data": [
                            {"yAxis": BACKTEST_MAX_DD_PCT,
                             "label": {"formatter": f"backtest max DD {BACKTEST_MAX_DD_PCT:.2f}%"},
                             "lineStyle": {"color": "#dc2626", "type": "dashed"}}]}}],
            "grid": {"left": 55, "right": 20, "top": 20, "bottom": 45},
        }).classes("w-full h-44").tooltip(
            "Current drawdown from the peak DEPOSIT-ADJUSTED value (pure trading performance); "
            "dashed line = backtest worst case")

    # allocation pie: strategy positions + SGOV + buffer cash, dual-currency on hover
    id_to_sym = {t["id"]: t["instrument"] for t in paper.open_trades()}
    base_to_usd = 1.0 / usd_to_base if usd_to_base else 0.0   # base ccy -> USD
    raw = []  # (short_name, base_value, usd_value)
    # FIXED 2026-08-05: dedupe by broker ticket before summing. When one instrument has
    # multiple layered paper trades funding the SAME aggregate broker position, a stale
    # ib_mirror row for an already-resolved trade could keep pointing at that same ticket
    # (root cause fixed in ib_exec.py::sync_closures(), which now closes such rows) --
    # this loop is defense-in-depth for the propagation window before that next runs, and
    # costs nothing for the normal one-ticket-per-pid case (MT5 always is; IB is once the
    # ib_mirror fix lands). Process open-trade pids first so the kept label is always the
    # genuinely-open trade, not a resolved duplicate.
    seen_tickets: set = set()
    for pid in sorted(positions, key=lambda k: k not in id_to_sym):
        p = positions[pid]
        ticket = p.get("ticket")
        if ticket is not None:
            if ticket in seen_tickets:
                continue
            seen_tickets.add(ticket)
        mv_usd = p["volume"] * p["open"] + p.get("profit", 0.0)
        # FIXED 2026-08-17: id_to_sym only covers paper_trades status='OPEN' rows -- a
        # position whose paper_trades row resolved (e.g. horizon-expiry) without the
        # broker-side close actually executing fell through to str(pid), a bare unlabeled
        # paper_id number on the chart. Confirmed live: 5 real LIVE positions (AMLP/CPER/
        # DBC/IWM/VNQ) had their correct dollar value included in the pie the whole time --
        # only the LABEL was ever wrong. ib_mirror's own local_symbol (carried on `p` by
        # live_positions() precisely for this) is correct regardless of paper_trades status.
        label = id_to_sym.get(pid) or p.get("symbol") or str(pid)
        raw.append((label, mv_usd * usd_to_base, mv_usd))
    if sgov_base > 0:
        raw.append((f"SGOV {sgov_yld}", sgov_base, sgov_base * base_to_usd))
    if cash is not None and cash > 0:
        raw.append(("Cash buffer", cash, cash * base_to_usd))
    total_base = sum(b for _, b, _ in raw) or 1.0
    # FULLY precomputed labels (USD actual + ccy converted + %) baked into the slice name
    # -> no ECharts {..} templates rendered; details sit on each slice's title, not the tooltip.
    slices = [{"value": round(b, 2),
               "name": f"{s} {b / total_base * 100:.0f}%\nUSD {u:,.0f} / {ccy} {b:,.0f}"}
              for s, b, u in raw]
    if slices:
        # MOBILE-FRIENDLY 2026-08-05: outside labels with leader lines (the previous style)
        # get crowded/overlapping on a narrow phone screen once there are more than ~4-5
        # slices -- switch to INSIDE labels (percent only, always fits within its own slice,
        # no leader lines competing for horizontal space) and move the full name/$ breakdown
        # into each slice's tooltip instead, which is tap-to-reveal on mobile rather than
        # needing screen width. media_query keeps the roomier outside-label style on desktop
        # (nicequi ui.echart accepts a plain ECharts option dict; NiceGUI's own responsive
        # container classes handle overall width, so only the pie's internal layout needs a
        # size-based switch, done here via ECharts' own "media" query support).
        # NOTE: ECharts' media-query responsive support requires the top-level option to be
        # wrapped as {baseOption: {...}, media: [...]} -- a flat option dict with "media" as
        # a sibling of "series" is silently ignored (no error, the query just never matches).
        ui.echart({
            "baseOption": {
                "tooltip": {"show": True, "trigger": "item", "formatter": "{b}"},
                "legend": {"show": False},
                "series": [{"type": "pie", "radius": ["35%", "60%"],
                            "center": ["50%", "50%"], "data": slices,
                            "label": {"show": True, "position": "outside", "fontSize": 9,
                                      "formatter": "{b}"},
                            "labelLine": {"show": True}}],
            },
            "media": [{
                "query": {"maxWidth": 480},
                "option": {
                    "series": [{"radius": ["30%", "55%"], "center": ["50%", "45%"],
                                "label": {"show": True, "position": "inside",
                                         "fontSize": 10, "color": "#fff",
                                         "formatter": "{d}%"},
                                "labelLine": {"show": False}}],
                },
            }],
        }).classes("w-full h-80").tooltip(
            f"Allocation — each slice labelled with USD (actual) + {ccy} (converted) + %. "
            "Tap a slice for details on a narrow screen.")


def _pending_reason(t: dict, room: float | None, eq: float | None,
                    earliest_free: str | None = None) -> tuple[str, str]:
    """Why a qualifying signal isn't showing as a confirmed position yet. Returns
    (message, status), status is one of:
      "placed"    -- a real order IS sitting at the broker, just hasn't filled yet.
      "retrying"  -- not at the broker yet, but this is TEMPORARY and self-resolving --
                     the system will automatically try again on its own, no action needed.
      "stuck"     -- will NOT resolve on its own; needs something to actually change
                     (the account growing) before this can ever place.

    REWORDED 2026-07-13 (previously a boolean): a boolean "already placed?" can't express
    the real difference between "blocked right now but will retry automatically" (a signal
    held back by PORTFOLIO_CAP room, or one that just hasn't had its next mirror cycle yet)
    and "genuinely stuck until the account grows" (a funding gap) -- the old universal
    "will never fill on its own" wording was factually WRONG for the first two cases
    (confirmed live: SPY/QQQ/IWM/DIA all correctly held back by the cap, all will place
    automatically the moment room frees up, none of them "never").

    `room` (PORTFOLIO_CAP room left, USD) and `eq` (equity, USD): both computed ONCE by
    the caller (active_panel()), NOT per-card here -- see the 2026-07-13 performance fix
    notes in HANDOFF.md if touching this again (per-card broker calls here once made the
    live dashboard fully unresponsive).

    `earliest_free` (ISO date string, 2026-07-14): the EARLIEST `horizon_end` among whatever
    is currently occupying deployed capital (confirmed positions + already-placed pending
    orders) -- a worst-case "by this date, SOMETHING currently deployed is guaranteed to
    resolve one way or another" estimate, since HORIZON_CAL forces a resolution (WIN/LOSS/
    EXPIRED) even if SL/TP never hit. Deliberately NOT a prediction of when THIS specific
    signal will place -- freeing one position's capital may or may not be enough room for
    this one, and SL/TP could resolve any of the occupying trades far sooner. Framed as a
    bound, not a forecast, to avoid false precision."""
    from dashboard.execution import broker as _bk
    if not _bk.is_ib():
        return "Broker isn't connected right now — this will be retried automatically once it reconnects.", "retrying"
    # ADDED 2026-08-21, user-requested: the actual cause of a real incident (HYG sitting
    # PENDING for hours, 2026-08-20) was the LLM board-scan pipeline itself being backed
    # off -- mirror_new() (which funds AND cancels pending signals) only ever runs after a
    # successful board scan, so a backoff blocks EVERYTHING downstream of it: portfolio
    # room, execution window, all of it become moot until this clears. Checked early,
    # right after "is the broker connected," since it's upstream of every other reason
    # below -- a signal that "has room" and "is inside its execution window" is still
    # stuck if the pipeline that would actually act on it never runs. Reads the exact
    # same cache key board_scan.py itself uses (not a re-derived guess).
    from dashboard.web import board_scan
    _backoff = board_scan._rate_limited_until()
    if _backoff:
        try:
            _bo_dt = dt.datetime.fromisoformat(_backoff)
            if _bo_dt.tzinfo is None:
                _bo_dt = _bo_dt.replace(tzinfo=dt.timezone.utc)  # legacy naive = UTC
            if dt.datetime.now(dt.timezone.utc) < _bo_dt:
                _bo_cst = _bo_dt.astimezone(dt.timezone(dt.timedelta(hours=8)))
                return (f"The AI board-scan pipeline is currently unavailable (provider "
                        f"backing off until {_bo_cst:%a %H:%M HKT}) — nothing new can be "
                        "placed or cancelled until it recovers. This isn't specific to this "
                        "signal.", "retrying")
        except ValueError:
            pass    # malformed cached value -- ignore and fall through to the normal checks
    # ADDED 2026-08-21: a signal on a key that's been RETIRED from the active universe
    # (e.g. the UCITS swap's old US tickers) isn't "waiting to be funded" at all -- it's
    # waiting to be CANCELLED (see ib_exec.py's mirror_new() retirement-cancel branch).
    # Different framing on purpose so it doesn't read as a normal funding delay.
    from dashboard.instruments import active_by_key, ETF_TRADED_BY_KEY
    if t["instrument"] not in ETF_TRADED_BY_KEY and active_by_key(t["instrument"]) is not None:
        return ("This instrument was retired from the active trading universe (replaced "
                "by its UCITS equivalent) — this signal will be automatically cancelled, "
                "not funded.", "retrying")
    from dashboard.core import paper
    from dashboard.data import contracts
    stop_per_share = abs(t["entry"] - t["sl"])
    if eq is None or stop_per_share <= 0:
        return "Broker isn't connected right now — this will be retried automatically once it reconnects.", "retrying"
    needed = contracts.min_equity_for_1_share(stop_per_share, paper.RISK_PER_TRADE)
    if eq < needed:
        return (f"Account isn't big enough yet to buy even 1 share of this at the current "
                f"risk setting (needs ~USD {needed:,.0f}, you have ~USD {eq:,.0f}) — this "
                f"will sit here until the account grows, it won't place on its own.", "stuck")
    if t["id"] in _bk.executed_ids():
        return ("Order is already sitting with the broker, just waiting to fill (e.g. it "
                "was placed outside market hours, or the fill is simply taking a moment) — "
                "check IBKR directly if you want the exact live order status.", "placed")
    # FOUND 2026-07-13: a signal correctly held back by PORTFOLIO_CAP's own room check
    # (confirmed live: SPY/QQQ/IWM/DIA all logged "<1 share at the risk/cap budget, SKIP"
    # while equity was already fully committed to other pending orders) is a NORMAL, expected,
    # SELF-RESOLVING state -- not stuck, not an error, just the risk budget doing its job.
    if room is not None and room < t["entry"]:
        # SHORTENED 2026-07-23: this used to repeat the full "nothing wrong here... it'll
        # place automatically..." framing sentence on EVERY card in this group -- with 3+
        # room-blocked signals on screen at once (a common state), that's the same paragraph
        # verbatim, 3+ times, for information that's already stated ONCE at the group level
        # (see _PENDING_SECTIONS["retrying"]'s header + tooltip in active_panel()). Each card
        # now shows only what's actually instrument-specific: the numbers and the ETA bound.
        eta = ""
        if earliest_free:
            try:
                d = dt.datetime.fromisoformat(earliest_free).date()
                eta = f" ETA (outer bound): {d} at the latest, could be sooner."
            except ValueError:
                pass
        return (f"Needs ~USD {t['entry']:,.0f}/share, ~USD {room:,.0f} of room left.{eta}",
               "retrying")
    # ADDED 2026-07-31: execution-window gate (ib_exec.within_entry_execution_window(),
    # 10:00am-3:30pm ET, entries only) -- give this its own message rather than falling
    # through to the generic "just logged" one below, which would be misleading outside
    # the window (it wasn't "just logged," it's deliberately waiting for the window).
    # FIXED 2026-08-21: was calling this with no instrument arg, so it always checked
    # NYSE hours regardless of which exchange the instrument actually trades on -- stale
    # since the LSE-aware version shipped for the UCITS swap instruments (ib_exec.py). An
    # LSE instrument's card was showing the wrong window entirely.
    from dashboard.execution import ib_exec
    if not ib_exec.within_entry_execution_window(instrument=t["instrument"]):
        inst = active_by_key(t["instrument"])
        window_txt = ("8:30am-4:00pm UK time (LSE)" if inst and inst.ib_exchange
                      else "10:00am-3:30pm ET (NYSE)")
        return (f"Outside the {window_txt} execution window (avoids the wider "
                "open/close spreads) — will place automatically once it opens.", "retrying")
    return "Just logged a moment ago — should reach the broker within the next check (about a minute).", "retrying"


def _current_price(t: dict, pos: dict | None) -> float:
    """The price _trade_card() displays and _unrealized_r() computes against.
    FIXED 2026-07-30: for an open position, STATE["live"]'s price is only genuinely fresh
    under MT5 (real tick data); for BROKER=ib it falls back to get_history()'s WEEKLY
    yfinance bars (scoring interval=1wk), stale by up to a week -- confirmed live: showed
    QQQ at 675.49 (last Tuesday's close) vs a real 664.37, silently wrong unrealized-R too.
    ib_exec.live_positions() now reports the broker's own live mark (ib.portfolio()'s
    marketPrice) as "current_price" -- prefer that whenever a real position exists; only
    fall back to STATE["live"] for PENDING signals (no broker position yet) or under MT5
    (which never sets current_price -- its STATE["live"] price is already tick-fresh)."""
    key = t["instrument"]
    live = service.STATE.get("live", {}).get(key)
    return (pos.get("current_price") if pos and pos.get("current_price") else
            (live["price"] if live else t["entry"]))


def _unrealized_pnl_native(t: dict, pos: dict | None, real_qty: float | None = None) -> float:
    """Dollar P&L in the instrument's own/native currency (USD for this project's ETFs),
    computed from THIS trade's OWN entry/price -- NOT pos["profit"] (IBKR's unrealizedPNL),
    which is the broker's AGGREGATE P&L across every layer accumulated into that con_id's
    position, not just this trade's own share. Same multi-layer bug class as _unrealized_r()
    above (see its docstring), just surfacing in the dollar figure instead of the R figure --
    confirmed live 2026-08-05: CPER showed a self-consistent +0.93R right next to a wildly
    inconsistent (HKD +12,980) because the R math had already been fixed to be trade-own, but
    this dollar figure was still reading the broker's blended 2-layer position profit (real
    broker position: 453 shares from an already-resolved older trade + 293 from this one,
    746 total, still sitting there because a paper trade resolving via the deterministic tick
    path does NOT itself sell the broker position -- that's real: this trade's own 293-share
    share was actually only ~HKD 2,370, a ~5.5x overstatement).

    FIXED 2026-08-06: this STILL multiplied by t["size_units"] (the fixed-$10,000-backtest-
    reference size, same one the "invested" line moved away from the same day) instead of the
    REAL broker-requested quantity -- a second instance of the exact bug class it was already
    fixing above, just left behind in this one spot. Confirmed live: EFA showed "invested:
    USD 205 (2 units)" (correctly using the real qty=2) right next to "unrealized: +2.11 R
    (HKD +1,647)" -- a HUGE dollar figure for a tiny position -- because this function was
    still multiplying by size_units=43.12 (real qty=2, a ~21.6x overstatement: 1,647 HKD
    should have been ~76 HKD). `real_qty` now defaults to `t["size_units"]` only when the
    caller doesn't have a real quantity to pass (mirrors _trade_card()'s own invested-line
    fallback -- same real-vs-reference distinction, same fallback rule, applied consistently
    everywhere a dollar figure is shown for a specific trade)."""
    price = _current_price(t, pos)
    entry = t["entry"]
    diff = (price - entry) if t["direction"] == "long" else (entry - price)
    qty = real_qty if real_qty is not None else t["size_units"]
    return diff * qty


def _unrealized_r(t: dict, pos: dict | None) -> float:
    """R-multiple of the CURRENT price against this trade's OWN entry/stop -- factored out
    of _trade_card() so the Active Trades sort-by-R control (2026-08-05) can never drift
    from what the card itself displays.
    FIXED 2026-08-05: this used to prefer pos["open"] (the REAL fill price) over the
    paper-recorded entry -- fine for a single-fill position (the two are nearly identical,
    differing only by tiny slippage), but WRONG once this instrument has multiple separate
    paper trades layered into ONE aggregate broker position (confirmed live: paper CPER had
    an older EXPIRED trade (entry 37.45) plus a newer OPEN one (entry 39.28, SL 38.17)
    sharing the same broker position -- pos["open"] is IBKR's own BLENDED avgCost across
    BOTH layers, 38.09, which has no relationship to this specific trade's own SL/TP.
    Pairing that blended price with THIS trade's own SL made the risk denominator collapse
    toward zero (38.09 sits almost exactly on this trade's 38.17 stop) and the displayed R
    explode to +24.3 -- a real trade with a completely ordinary ~+0.76R position. R-multiple
    math is only meaningful relative to the SAME trade's own entry/stop pair, never a
    cross-layer blended average -- use the paper-recorded entry unconditionally."""
    price = _current_price(t, pos)
    entry = t["entry"]
    risk = abs(entry - t["sl"]) or 1e-9
    return ((price - entry) if t["direction"] == "long" else (entry - price)) / risk


def _trade_card(t: dict, pos: dict | None, reason: str | None = None,
                status: str | None = None, real_qty: float | None = None) -> None:
    key = t["instrument"]
    price = _current_price(t, pos)
    # entry: always this trade's OWN recorded price, never a broker cross-layer blended
    # average -- keeps both the R math below and the "entry X · SL Y · TP Z" display line
    # internally consistent (X paired with the SL/TP it was actually set relative to). See
    # _unrealized_r()'s docstring for the full 2026-08-05 bug this fixed.
    entry = t["entry"]
    ur = _unrealized_r(t, pos)
    from dashboard.execution import broker as _bk
    if pos:
        col = "bg-green-1" if ur >= 0 else "bg-red-1"
        card_extra = ""
    else:                                          # PENDING: unmistakably different look
        col = "bg-grey-2"
        card_extra = " border-dashed border-2 border-grey-5 opacity-80"
    # ADDED 2026-07-14: surface macro_linkage as a badge on the card itself, not only in the
    # Details dialog -- the whole point of forcing this field (see board_scan.py) was to make
    # cross-asset macro risk visible, which a buried dialog undermines. Only shown when there's
    # something to say (skips the common "none material" case to avoid clutter).
    _mlink = (t.get("macro_linkage") or "").strip()
    _has_macro_flag = _mlink and _mlink.lower() not in ("none material", "none", "n/a", "")
    with ui.card().classes(f"min-w-[210px] grow {col}{card_extra}"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.row().classes("items-baseline gap-1"):
                ui.label(active_by_key(key).name).classes("font-bold")
                ui.label(key).classes("text-xs text-grey-6 font-mono")
            with ui.row().classes("items-center gap-1"):
                if _has_macro_flag:
                    ui.badge("🌐 macro", color="purple").classes("text-xs")\
                        .tooltip(_mlink)
                ui.badge(t["direction"],
                         color="positive" if t["direction"] == "long" else "negative")
        if not pos:
            ui.badge("⏳ PENDING", color="grey-7").classes("text-xs")
        ui.label(f"{price:,.4f}").classes("text-base")
        # FIXED 2026-08-06: this used to show t["size_units"] * entry -- paper.py's OWN
        # sizing, against a fixed $10,000 backtest-reference account, NOT real money -- and a
        # user correctly called it out as misleading once it was labelled "invested" (it once
        # read "invested: USD 5,132" for a real position that only ever cost USD 3,084 at the
        # broker). Now uses `real_qty` (the ACTUAL quantity ib_exec.py/executor.py requested
        # from the broker for THIS specific trade, read from ib_mirror/mt5_mirror -- present
        # once a real order has been placed, filled or not) whenever it exists -- exact, no
        # estimation, and per-trade (not the pie chart's aggregate across multiple layers on
        # the same instrument). Only falls back to the backtest-reference figure for a signal
        # that has NEVER been placed at the broker at all, and says so explicitly rather than
        # calling it "invested" -- nothing has actually been invested yet in that case.
        if real_qty is not None:
            ui.label(f"invested: USD {real_qty * entry:,.0f} "
                    f"({real_qty:,.0f} units)").classes("text-xs text-grey-6").tooltip(
                "The real quantity requested from the broker for this trade (from "
                f"{_bk.mirror_table()}) x entry price -- matches what you'd see in "
                f"{_bk.name()} for this order.")
        else:
            ui.label(f"~USD {t['size_units'] * entry:,.0f} reference size "
                    f"({t['size_units']:,.0f} units, not yet placed)").classes(
                "text-xs text-grey-6").tooltip(
                "No real order exists at the broker yet for this signal -- this is a "
                "backtest-consistency reference size (sized against this system's fixed "
                "$10,000 reference account), not a prediction of the real quantity. The real "
                "size is set independently, against your actual account equity, once this "
                "signal actually places.")
        spark = service.STATE.get("spark", {}).get(key)
        if spark:                                  # same sparkline as Top Opportunities
            up = spark[-1] >= spark[0]
            ui.html(_sparkline_svg(spark, up, h=32)).classes("w-full")
        if pos:                                   # P&L in account base ccy (HKD)
            from dashboard.data import ib_client
            _acct = service.STATE.get("account") or {}
            _ccy = _acct.get("_ccy", "")
            _f = 1.0 / ib_client._PEG_USD_PER.get(_ccy, 1.0)
            pnl = f"  ({_ccy} {_unrealized_pnl_native(t, pos, real_qty) * _f:+,.0f})"
            # ADDED 2026-07-14: after this session's CWB/ASHR confusion (local records
            # showing a status the broker no longer agreed with), show WHEN this position
            # was last actually cross-checked against the broker, not just trust the display
            # is current. Reuses service.STATE["last_cheap"] -- broker.live_positions() (the
            # call that populates `pos` here) runs as part of the same refresh_cheap() cycle
            # that sets this timestamp, so it's already exactly "how fresh is this position
            # data," no new tracking needed.
            # SHORTENED 2026-07-24: this used to be its own always-visible "verified vs
            # broker: Xm ago" text line under every open card -- with 7-10 cards, that's a
            # full extra line repeated 7-10 times for something that's normally just "just
            # now" and rarely worth a second look. Now a small checkmark icon inline with the
            # unrealized figure; the same freshness detail is one hover away in its tooltip.
            with ui.row().classes("items-center gap-1"):
                ui.label(f"unrealized: {ur:+.2f} R{pnl}").classes("text-sm font-bold")
                _last_cheap = service.STATE.get("last_cheap")
                if _last_cheap:
                    _age_min = (dt.datetime.now() - _last_cheap).total_seconds() / 60
                    _age_txt = f"{_age_min:.0f}m ago" if _age_min >= 1 else "just now"
                    ui.icon("verified", size="xs").classes("text-green-6")\
                        .tooltip(f"verified vs broker: {_age_txt}")
        else:
            # (reason, status) now computed ONCE per pending trade by active_panel() --
            # see its 2026-07-13 grouping note -- and passed straight through here, not
            # recomputed per card.
            _colour = {"placed": "text-grey-8", "retrying": "text-blue-8",
                      "stuck": "text-orange-8"}[status]
            ui.label(reason).classes(f"text-xs {_colour}")
        # NOTE: "unconfirmed" here means "no broker fill matched yet" -- has nothing to do
        # with PAPER-vs-LIVE mode (this branch is reached in both), so don't say "paper".
        # FIXED 2026-08-05: used to read f"{_bk.name()} fill" and the entry above used to show
        # pos["open"] (the broker's own price) when confirmed -- now entry is always this
        # trade's own recorded price (see the entry= fix above, needed to keep R-multiple math
        # correct for multi-layer positions), so labelling it "IBKR fill" would be a lie once
        # pos["open"] and t["entry"] diverge. "confirmed" still tells the user this trade has
        # been matched to a real broker position, without claiming the number shown is the
        # broker's own blended price.
        src = "confirmed" if pos else "logged, unconfirmed"
        ui.label(f"entry {entry:.4f} ({src}) · SL {t['sl']:.4f} · TP {t['tp']:.4f}")\
            .classes("text-xs text-grey-7")
        # SHORTENED 2026-07-24: dropped the internal paper-journal "#{id}" from this line --
        # it's an internal DB reference a live user never needs; the broker ticket (when
        # there is one) is the externally-meaningful number that actually matches IBKR's own
        # display, so that's the one kept.
        tag = (f" · ticket {pos['ticket']}" if pos
              else (" (order placed, unfilled)" if t["id"] in _bk.executed_ids()
                    else f" (not on {_bk.name()})"))
        ui.label(f"{t['method']} · opened {_fmt_ts(t['ts'])}{tag}")\
            .classes("text-xs text-grey-6")
        with ui.row().classes("gap-1 items-center"):
            ui.button("Details", on_click=lambda k=key: _open_detail(k))\
                .props("flat dense").classes("text-xs")
            # ADDED 2026-07-30: per-trade manual controls, PENDING trades only -- a filled
            # (pos is not None) trade is already protected by its own broker-side SL/TP
            # bracket, so neither action applies there.
            if not pos:
                _already_placed = t["id"] in _bk.executed_ids()
                if not _already_placed:
                    # Pause only makes sense before mirror_new() has dispatched the trade --
                    # once a real order is resting at the broker, mirror_new()'s
                    # `if t["id"] in done: continue` never looks at manual_paused again, so
                    # the toggle would silently do nothing. Withdraw (below) still works then
                    # because it cancels the broker order directly.
                    _paused = bool(t.get("manual_paused"))
                    def _toggle_pause(tid=t["id"], inst=key, want=not bool(t.get("manual_paused"))) -> None:
                        from dashboard.core import paper as _paper
                        from dashboard.core import notable_events
                        _paper.set_trade_paused(tid, want)
                        # ADDED 2026-07-30: persisted to the same changelog the "Recent
                        # notable events" retrospective panel + Telegram already read, so a
                        # manual pause/resume leaves the same kind of retro trail as every
                        # automated gate action -- previously only a transient ui.notify()
                        # toast, gone the moment the page refreshed.
                        notable_events.record(
                            f"{inst} (#{tid}): manually {'paused' if want else 'resumed'} "
                            "(pending trade)")
                        ui.notify(f"{inst}: {'paused' if want else 'resumed'}")
                        active_panel.refresh()
                    ui.button("Resume" if _paused else "Pause",
                             icon="play_arrow" if _paused else "pause",
                             on_click=_toggle_pause).props("flat dense").classes("text-xs")\
                        .tooltip("Resume automatic funding -- picks up again next cycle"
                                if _paused else
                                "Hold this trade back from funding -- reversible, stays "
                                "queued, resume any time")
                def _withdraw(tid=t["id"], inst=key, placed=_already_placed) -> None:
                    from dashboard.core import paper as _paper
                    from dashboard.core import notable_events
                    if placed and _bk.is_ib():
                        from dashboard.execution import ib_exec
                        ib_exec.cancel_pending_order(tid)
                    _paper.withdraw_trade(tid)
                    # ADDED 2026-07-30: same retro trail (and level -- plain "info", no
                    # Telegram push) as the tech-pause gate's own auto-cancellations
                    # (ib_exec.mirror_new()) -- the user is already on the dashboard,
                    # deliberately clicking this, so a phone buzz would just be noise; the
                    # persisted record is what matters for later review.
                    notable_events.record(
                        f"{inst} (#{tid}): manually withdrawn" +
                        (" (resting broker order also cancelled)" if placed else ""))
                    ui.notify(f"{inst}: withdrawn", type="warning")
                    active_panel.refresh()
                ui.button("Withdraw", icon="close", on_click=_withdraw)\
                    .props("flat dense color=negative").classes("text-xs")\
                    .tooltip("Cancel this trade permanently" +
                            (" (also cancels the resting broker order)"
                             if _already_placed else ""))


def _fundable_count(eq: float | None) -> tuple[int | None, int]:
    """How many of the active universe's instruments could size >=1 share RIGHT NOW at
    current equity + risk/trade. Explains the gap between the backtest's SIGNAL frequency
    (BACKTEST_SIGNAL_FREQ_YR, fixed at the account's target/planned scale) and the account's
    actual FILL frequency today -- a cheap/low-ATR instrument (e.g. a bond ETF) sizes easily
    on a small account, but an expensive/high-ATR one (e.g. SPY, QQQ) can eat most of a small
    account's risk budget in one position, so many qualifying signals go unfunded until the
    account grows. First element is None if equity is unavailable (e.g. broker disconnected)
    -- distinct from 0 fundable, which is a real (if grim) answer.

    `eq` is computed ONCE by the caller (active_panel()), not here -- see _pending_reason()'s
    2026-07-13 docstring for why per-call broker round-trips in a render path are a real
    performance risk, not just a style nit."""
    from dashboard.core import paper
    from dashboard.data import contracts
    from dashboard.instruments import active_universe
    universe = active_universe()
    if eq is None or not universe:
        return None, len(universe)
    fundable = 0
    for inst in universe:
        score = service.STATE.get("scores", {}).get(inst.key)
        if not score:
            continue
        atr = score.facts.get("atr14") or 0.0
        stop_per_share = paper.SL_ATR_MULT * atr
        if stop_per_share <= 0:
            continue
        needed = contracts.min_equity_for_1_share(stop_per_share, paper.RISK_PER_TRADE)
        if eq >= needed:
            fundable += 1
    return fundable, len(universe)


def _active_sort_value(t: dict, pos: dict | None, real_qty_by_id: dict):
    """Value _sort_active() sorts on, per SETTINGS["active_sort"]. "r"/"profit" only mean
    anything for a CONFIRMED trade (a real position); a pending trade (pos is None) has no
    live figure yet, so it gets a constant sentinel -- combined with sorted()'s stability,
    every pending trade in a group ties and the group's original order is left untouched
    (matches the design: sorting by R/profit visibly reorders Confirmed, not the pending
    groups, rather than pending trades all clumping at one end)."""
    key = SETTINGS.get("active_sort", "entry_date")
    if key == "r":
        return _unrealized_r(t, pos) if pos else float("-inf")
    if key == "profit":
        # FIXED 2026-08-06: same real-qty fix as the "invested" case just below.
        return (_unrealized_pnl_native(t, pos, real_qty_by_id.get(t["id"]))
                if pos else float("-inf"))
    if key == "invested":
        # FIXED 2026-08-06: matches _trade_card()'s own "invested" line -- the REAL
        # broker-requested quantity when one exists, only falling back to the backtest-
        # reference size_units for a signal that's never been placed at all.
        qty = real_qty_by_id.get(t["id"])
        return (qty if qty is not None else t["size_units"]) * t["entry"]
    return t["ts"]                       # entry_date -- ISO strings sort chronologically


def _sort_active(items: list, get_trade, get_pos, real_qty_by_id: dict) -> list:
    """Sort a group's card list by the current Active Trades sort control. `get_trade`/
    `get_pos` adapt this to both shapes used below: plain trade dicts (confirmed) and
    (trade, reason_msg) tuples (each pending sub-group)."""
    reverse = SETTINGS.get("active_sort_dir", "desc") == "desc"
    return sorted(items, key=lambda it: _active_sort_value(get_trade(it), get_pos(it),
                                                            real_qty_by_id),
                 reverse=reverse)


@ui.refreshable
def active_panel() -> None:
    """Open positions shown on the Board with live unrealized P&L in R. Splits
    CONFIRMED (a real, broker-mirrored position) from PENDING (a signal that fired
    and was logged, but never actually got sized/placed on the broker -- e.g. an
    account too small to fund it) -- these used to be silently counted together as
    one misleading "Active Trades (N)" total with no distinction."""
    from dashboard.core import paper
    from dashboard.execution import broker as _bk
    open_t = paper.open_trades()
    positions = service.STATE.get("positions", {})
    # ADDED 2026-08-06: REAL per-trade order quantity (the actual size ib_exec.py/executor.py
    # requested from the broker for THIS specific trade -- present once a real order has been
    # placed, whether filled yet or not) -- see _trade_card()'s "invested" line, which uses
    # this instead of t["size_units"] (a separate, fixed-$10,000-backtest-reference-scale
    # figure that isn't real money -- confirmed live to differ from the real broker qty by
    # anywhere from ~0.5x to ~13x, which a user correctly flagged as misleading once it was
    # labelled "invested"). One query for the whole panel, not per-card.
    with paper._LOCK, paper._conn() as c:
        real_qty_by_id = dict(c.execute(
            f"SELECT paper_id, {_bk.mirror_qty_column()} FROM {_bk.mirror_table()}").fetchall())
    confirmed = [t for t in open_t if positions.get(t["id"])]
    pending = [t for t in open_t if not positions.get(t["id"])]
    hdr = f"Active Trades ({len(confirmed)} open"
    hdr += f" · {len(pending)} pending)" if pending else ")"
    with ui.row().classes("items-center justify-between w-full flex-wrap gap-2"):
        ui.label(hdr).classes("text-lg font-bold")
        # ADDED 2026-08-05: sorts WITHIN each existing group below (Confirmed, then each
        # pending sub-group) rather than flattening them into one list -- the grouping itself
        # is meaningful (see the docstring above + the 2026-07-13 pending-grouping note), a
        # global sort would bury that distinction.
        def _set_active_sort(e) -> None:
            SETTINGS["active_sort"] = e.value
            _save_settings()
            active_panel.refresh()
        def _toggle_active_sort_dir() -> None:
            SETTINGS["active_sort_dir"] = ("asc" if SETTINGS.get("active_sort_dir", "desc") == "desc"
                                           else "desc")
            _save_settings()
            active_panel.refresh()
        with ui.row().classes("items-center gap-1"):
            ui.label("Sort by:").classes("text-xs text-grey-6")
            ui.select(ACTIVE_SORT_KEYS, value=SETTINGS.get("active_sort", "entry_date"),
                     on_change=_set_active_sort).props("dense options-dense").classes("text-xs w-40")
            _desc = SETTINGS.get("active_sort_dir", "desc") == "desc"
            ui.button(icon="arrow_downward" if _desc else "arrow_upward",
                     on_click=_toggle_active_sort_dir).props("flat dense round")\
                .tooltip("Descending (biggest/most recent first)" if _desc
                        else "Ascending (smallest/oldest first)")
    # computed ONCE for the whole render -- both _fundable_count() and _pending_reason()
    # used to each call _bk.equity_usd() independently (a real broker round-trip), once per
    # pending CARD for the latter; see the 2026-07-13 fix note on _pending_reason().
    # ADDED 2026-08-25: reads the cache service.refresh_cheap() populates instead of calling
    # equity_usd() live -- this function runs synchronously inside main_page()'s HTTP render
    # path; a live call blocks the WHOLE event loop for up to 30s when the gateway is
    # slow/unreachable (same root cause confirmed via a live faulthandler dump for
    # health_banner(), see its 2026-08-25 note -- fixing this call site the same way).
    eq = service.STATE.get("equity_usd") if _bk.is_ib() else None
    if _bk.is_ib():
        fundable, total = _fundable_count(eq)
        freq = (f"Signal freq (backtest): ~{BACKTEST_SIGNAL_FREQ_YR}/yr "
                f"(~{BACKTEST_SIGNAL_FREQ_WK:.1f}/wk)")
        if fundable is not None:
            freq += f"  ·  Fundable now: {fundable}/{total} ETFs at current equity"
        ui.label(freq).classes("text-xs text-grey-6").tooltip(
            "The backtest's signal frequency is how often the strategy finds a qualifying "
            "setup across the whole universe -- NOT how often trades actually FILL. A small "
            "account can't size expensive/high-volatility instruments (e.g. SPY, QQQ) even "
            "when they qualify, so real fill frequency is lower until the account grows -- "
            "see the Pending sections below (not always a funding gap -- could also be "
            "waiting on the risk budget or a broker fill).")
    if not open_t:
        ui.label("No open positions. Setups are logged automatically from "
                 "qualifying signals.").classes("text-sm text-grey")
        return
    if confirmed:
        confirmed = _sort_active(confirmed, lambda t: t, lambda t: positions.get(t["id"]),
                                 real_qty_by_id)
        with ui.row().classes("w-full flex-wrap gap-3"):
            for t in confirmed:
                _trade_card(t, positions.get(t["id"]), real_qty=real_qty_by_id.get(t["id"]))
    # FLAGGED (2026-08-17): a real broker position (in `positions`, sourced from ib_mirror)
    # with NO corresponding OPEN paper_trades row -- the strategy resolved/expired the trade
    # (e.g. horizon-expiry) but the broker-side close never actually executed. Confirmed live
    # on LIVE: 5 real positions (AMLP/CPER/DBC/IWM/VNQ) sat with zero visibility ANYWHERE in
    # this panel for 3+ days -- correctly included in every portfolio total (`positions` came
    # from ib_mirror, which was accurate) but with nothing rendering a card for them at all,
    # since this whole panel only ever iterates `open_t` (paper_trades status='OPEN'). Shown
    # READ-ONLY here (no SL/TP/pause/withdraw controls -- there's no live paper_trades record
    # to act through) so a real, currently-held position is never silently invisible again.
    # Does NOT touch paper_trades or submit any order -- purely a visibility fix.
    flagged = {pid: p for pid, p in positions.items() if pid not in {t["id"] for t in open_t}}
    if flagged:
        ui.label(f"⚠️ Flagged positions ({len(flagged)}) — broker holds these for real, but "
                 "the local strategy record shows them already resolved. Read-only; needs "
                 "manual review (see HANDOFF.md 2026-08-17).").classes(
            "text-sm font-bold text-orange-9 mt-2")
        with ui.row().classes("w-full flex-wrap gap-3"):
            for pid, p in sorted(flagged.items(), key=lambda kv: kv[1].get("symbol") or str(kv[0])):
                sym = p.get("symbol") or f"id {pid}"
                with ui.card().classes("p-3").style("border: 1px solid orange"):
                    ui.label(sym).classes("font-bold")
                    ui.label(f"{p.get('direction', 'long')} · {p.get('volume', 0):.0f} units "
                             f"@ avg {p.get('open', 0):.4f}").classes("text-xs text-grey-7")
                    cp = p.get("current_price")
                    ui.label(f"current: {cp:.4f}" if cp else "current: —").classes(
                        "text-xs text-grey-7")
                    profit = p.get("profit", 0.0)
                    ui.label(f"unrealized: USD {profit:+,.2f}").classes(
                        "text-sm font-bold " + ("text-green" if profit >= 0 else "text-red"))
    if pending:
        # GROUPED BY REASON CATEGORY (2026-07-13, replacing one flat "Pending" list):
        # user feedback was that lumping "a real order is genuinely waiting to fill" together
        # with "correctly held back by the risk budget, will place itself automatically" under
        # one undifferentiated heading required reading every card's own text to tell them
        # apart -- three separate sub-sections make the distinction visible at a glance
        # instead. `room`/`eq` computed ONCE for the whole panel (not per-card -- see
        # _pending_reason()'s docstring: per-card was several real broker round-trips EACH,
        # confirmed live to make the whole dashboard unresponsive with several pending cards
        # on screen), and (reason, status) computed once per trade here, not inside
        # _trade_card() -- passed straight through as plain values.
        # ADDED 2026-08-25: same cache-read fix as `eq` above -- was a live blocking call.
        room = service.STATE.get("portfolio_room_usd") if _bk.is_ib() else None
        # ADDED 2026-07-14: worst-case bound on when SOMETHING currently deployed resolves
        # (see _pending_reason()'s earliest_free docstring) -- computed from confirmed
        # positions + already-placed pending orders, ONCE, before the grouping loop (which
        # is what determines "placed" status per trade, but this needs that answer for ALL
        # pending trades up front, hence the separate executed_ids() pass here -- a local DB
        # read, not a broker round-trip, so this doesn't reintroduce the 2026-07-13
        # per-card-broker-call problem).
        executed_ids = _bk.executed_ids() if _bk.is_ib() else set()
        _occupying = confirmed + [t for t in pending if t["id"] in executed_ids]
        _horizons = [t["horizon_end"] for t in _occupying if t.get("horizon_end")]
        earliest_free = min(_horizons) if _horizons else None   # ISO strings sort chronologically
        groups: dict[str, list[tuple[dict, str]]] = {"placed": [], "retrying": [], "stuck": []}
        for t in pending:
            msg, status = _pending_reason(t, room, eq, earliest_free)
            groups[status].append((t, msg))
        _PENDING_SECTIONS = {
            "placed": ("Waiting to fill", "text-grey-7",
                       "Real orders already sitting with the broker — just haven't filled "
                       "yet (e.g. placed outside market hours, or the fill is simply taking "
                       "a moment). Check IBKR directly for the exact live order status."),
            "retrying": ("On hold — will retry automatically", "text-blue-7",
                        "Nothing to do here. These are held back on purpose right now (the "
                        "risk budget is fully committed elsewhere, or this one just hasn't "
                        "had its next check yet) and will place themselves the moment room "
                        "frees up — no action needed."),
            "stuck": ("Needs a bigger account", "text-orange-7",
                     "These won't place on their own — the account isn't big enough yet to "
                     "size even 1 share of this at the configured risk. Will sit here until "
                     "the account grows."),
        }
        # 2026-07-23: each group used to render as a full "text-sm font-bold" heading line --
        # 3 of those stacked (when all 3 categories are present) is a lot of heading-weight
        # text just to label pending trades. Keeps the AT-A-GLANCE grouping the 2026-07-13 fix
        # was FOR (that's still real -- see its comment above), just as a small chip instead
        # of a full heading line: same color-coded distinction, much less visual weight.
        _chip_color = {"placed": "grey-7", "retrying": "blue-7", "stuck": "orange-7"}
        with ui.row().classes("items-center gap-2 mt-2"):
            for _status in ("placed", "retrying", "stuck"):
                items = groups[_status]
                if not items:
                    continue
                _label, _colour, _tip = _PENDING_SECTIONS[_status]
                ui.badge(f"{_label} ({len(items)})", color=_chip_color[_status])\
                    .classes("text-xs").tooltip(_tip)
        for _status in ("placed", "retrying", "stuck"):
            items = _sort_active(groups[_status], lambda it: it[0], lambda it: None,
                                 real_qty_by_id)
            if not items:
                continue
            with ui.row().classes("w-full flex-wrap gap-3"):
                for t, msg in items:
                    _trade_card(t, None, reason=msg, status=_status,
                               real_qty=real_qty_by_id.get(t["id"]))


@ui.refreshable
def retrospective_panel() -> None:
    """Live equity curve + constraint scorecard for the forward test."""
    from dashboard.core import paper
    from dashboard.core import journal
    from dashboard.web.retrospective import equity_curve, _demo_executed_ids, confidence_calibration

    trades = paper.all_trades()
    # broker truth: KPIs/equity from trades the demo ACTUALLY executed (have an
    # MT5 order). Signals never sent to the broker don't count here.
    demo_ids = _demo_executed_ids()
    closed = [t for t in trades if t["status"] != "OPEN" and t["id"] in demo_ids]
    rs = [t["realized_r"] for t in closed]
    s = paper.stats(rs)
    curve, max_dd = equity_curve(closed)

    with ui.row().classes("items-center justify-between w-full"):
        ui.label("Retrospective — KPIs & Constraints").classes("text-lg font-bold")
        ui.button("Export full report", icon="download",
                  on_click=_export_retrospective).props("flat dense")
    from dashboard.execution import broker as _bk
    ui.label(f"KPIs/equity are over {_bk.name()}-EXECUTED trades only (real broker "
             "fills) — signals never placed are excluded. Constraint scorecard "
             "counts how often each gate blocked a candidate.")\
        .classes("text-xs text-grey-6")

    # KPI cards
    with ui.row().classes("w-full flex-wrap gap-3"):
        def _kpi(title: str, value: str, sub: str, good: bool | None = None) -> None:
            col = ("bg-green-1" if good else "bg-red-1") if good is not None else ""
            with ui.card().classes(f"min-w-[170px] {col}"):
                ui.label(title).classes("text-xs text-grey-7")
                ui.label(value).classes("text-base font-bold")
                ui.label(sub).classes("text-xs text-grey-6")
        total_r = curve[-1] if curve else 0.0
        _trust_sub = f"n={s['n']} · " + ("trustworthy" if s["trustworthy"] else "≥30 to trust")
        _kpi("Expectancy", f"{s['expectancy_R']:+.3f} R", _trust_sub,
             good=(s["expectancy_R"] > 0) if s["n"] else None)
        _kpi("Total / equity", f"{total_r:+.2f} R",
             f"{total_r*paper.RISK_PER_TRADE:+.2%} acct", good=(total_r > 0) if curve else None)
        _kpi("Max drawdown", f"{max_dd:.2f} R", _trust_sub,
             good=(max_dd == 0) if curve else None)
        _kpi("Win rate", f"{s['win_rate']:.0%}",
             "≥30 to trust" if not s["trustworthy"] else "trustworthy")

    # equity curve
    if curve:
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": list(range(1, len(curve) + 1)),
                      "name": "closed trade #"},
            "yAxis": {"type": "value", "name": "cumulative R"},
            "series": [{"type": "line", "data": curve, "smooth": True,
                        "areaStyle": {}, "lineStyle": {"width": 2}}],
            "grid": {"left": 50, "right": 20, "top": 30, "bottom": 40},
        }).classes("w-full h-64")
        # ADDED 2026-08-26: underwater view of the SAME R-curve -- depth (maxDD) was already
        # a KPI card, but drawdown DURATION is at least as informative ("an -8% DD that
        # recovers in 3 weeks vs one that drags 8 months", README 2026-08-06) and only
        # visible as a shape, not a number. Shaded area = how far below the running peak
        # (in R) the account sat after each closed trade.
        _peak, _under = None, []
        for _c in curve:
            _peak = _c if _peak is None else max(_peak, _c)
            _under.append(round(_c - _peak, 3))
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": list(range(1, len(curve) + 1)),
                      "name": "closed trade #"},
            "yAxis": {"type": "value", "name": "R below peak", "max": 0},
            "series": [{"type": "line", "data": _under, "smooth": True, "areaStyle": {},
                        "lineStyle": {"width": 1.5}, "itemStyle": {"color": "#dc2626"}}],
            "grid": {"left": 50, "right": 20, "top": 20, "bottom": 40},
        }).classes("w-full h-36").tooltip(
            "Distance below the running equity peak after each closed trade (in R). "
            "Wide/long shaded regions are long, deep drawdowns -- the behavioral cost "
            "of holding through them.")
    else:
        ui.label("No closed trades yet — the equity curve appears as trades settle.")\
            .classes("text-sm text-grey")

    # ADDED 2026-08-26: live-vs-backtest drift check (dashboard/web/drift.py) --
    # research/live_vs_backtest.py's methodology surfaced continuously instead of living
    # only as a manually-run script. Shows real per-strategy expectancy/win-rate next to
    # the cached backtest reference, with binomial/t-test p-values once scipy can compute
    # them; the heavy reference computation runs ONLY on explicit button click (it
    # downloads full history for the whole universe -- minutes), then caches forever.
    with ui.expansion("Live vs backtest drift",
                      icon="compare_arrows",
                      caption="are real closed trades still behaving like the backtest?")\
            .classes("w-full mt-2"):
        from dashboard.web import drift
        try:
            from dashboard.core.sleeve import SLEEVE_METHOD
            _raw = {"core": [], "sleeve": []}
            for _t in closed:
                if _t["realized_r"] is None:
                    continue
                _raw["sleeve" if _t.get("method") == SLEEVE_METHOD else "core"].append(
                    float(_t["realized_r"]))
            split = {k: paper.stats(v) for k, v in _raw.items()}
            ref, ref_ts = drift.cached_reference()
            for strat in ("core", "sleeve"):
                st, rs = split.get(strat) or {}, _raw[strat]
                if not st.get("n"):
                    continue
                _good = st["expectancy_R"] > 0
                with ui.row().classes("items-baseline gap-4 flex-wrap w-full"):
                    ui.badge(strat.upper(),
                             color="blue" if strat == "core" else "purple")
                    ui.label(f"n={st['n']} · exp {st['expectancy_R']:+.3f}R · "
                             f"win {st['win_rate']:.0%}")\
                        .classes("text-sm " + ("text-green" if _good else "text-red"))
                    if ref and ref.get("n"):
                        cmp_ = drift.compare(st, rs, ref)
                        ui.label(f"vs backtest exp {ref['expectancy_R']:+.3f}R / "
                                 f"win {ref['win_rate']:.0%} "
                                 f"(n={ref['n']}, computed {ref.get('computed_ts', '?')})")\
                            .classes("text-xs text-grey-6")
                        if cmp_.get("win_p") is not None:
                            _flag = (cmp_["win_p"] < 0.05 or
                                     (cmp_.get("exp_p") is not None and cmp_["exp_p"] < 0.05))
                            ui.label(("⚠ diverging" if _flag else "consistent"))\
                                .classes("text-xs font-bold " +
                                         ("text-orange-10" if _flag else "text-grey-6"))\
                                .tooltip(f"binomial win-rate p={cmp_['win_p']:.3f}" +
                                         (f", expectancy t-test p={cmp_['exp_p']:.3f}"
                                          if cmp_.get("exp_p") is not None else "") +
                                         ". p<0.05 on either = live measurably differs "
                                         "from the validated expectation -> investigate "
                                         "(per the forward-test protocol, not panic).")
                if st.get("n") and not st.get("trustworthy"):
                    ui.label(f"⚠ n={st['n']} < 30 — provisional, per the project's own "
                             "trustworthiness bar").classes("text-xs text-orange-8")
            if ref is None:
                ui.label("No cached backtest reference yet.")\
                    .classes("text-xs text-grey-6")

            _drift_busy = {"flag": False}

            async def _compute_ref() -> None:
                if _drift_busy["flag"]:
                    return
                _drift_busy["flag"] = True
                btn.props("loading")
                try:
                    await run.io_bound(drift.compute_reference)
                    retrospective_panel.refresh()
                finally:
                    _drift_busy["flag"] = False
            btn = ui.button("Compute backtest reference", icon="calculate",
                            on_click=_compute_ref)\
                .props("flat dense size=sm")\
                .tooltip("Downloads full weekly history for the whole active universe and "
                         "re-runs the deployed-config portfolio backtest (SAME methodology "
                         "as research/live_vs_backtest.py). Takes MINUTES -- one-time, "
                         "then cached until the config fingerprint changes.")
        except Exception as e:                             # noqa: BLE001
            ui.label(f"drift check unavailable: {e}").classes("text-xs text-grey-6")


    # 2026-07-23: the four sections below (confidence calibration, monthly attribution,
    # constraint scorecard, recent events) are analysis you check occasionally, not a live
    # monitor -- they used to stack as 4 always-visible headed sections, each with its own
    # bold heading AND a full explanatory sentence underneath, adding up to a long scroll
    # past this tab's actual headline KPIs/equity curve every single visit. Collapsed into
    # expansions (default closed) with a short caption so you can still tell what's inside
    # without opening it; the full explanation moved into each section's own tooltip/first
    # line instead of always-visible subtext.

    # ADDED 2026-07-14: confidence calibration -- retrospective.confidence_calibration()
    # already existed (used by the text-export report) but was never surfaced live, so the
    # question "is the LLM's confidence actually predictive?" required generating a report.
    # A bar chart of realized expectancy per confidence band answers it at a glance: if
    # higher bands don't show higher expectancy, the confidence gate is noise, not signal.
    with ui.expansion("LLM confidence calibration", icon="psychology",
                      caption="is the LLM's confidence number actually predictive?")\
            .classes("w-full mt-2"):
        cal = confidence_calibration(closed)
        if cal:
            # NOTE: no custom JS tooltip formatter -- ui.echart() JSON-serializes the whole
            # options dict (confirmed: no precedent for a JS-function-string formatter
            # anywhere else in this file), so a formatter string here would arrive as an
            # inert STRING, not an executable function. Same fix as the portfolio pie chart
            # elsewhere in this file: bake the extra info (win rate, n) directly into the
            # x-axis label text instead of relying on a callback.
            _labels = [f"{c['band']}\nwin {c['win']:.0%}  n={c['n']}" for c in cal]
            ui.echart({
                "tooltip": {"trigger": "axis"},
                "xAxis": {"type": "category", "data": _labels, "name": "LLM confidence",
                          "axisLabel": {"fontSize": 10}},
                "yAxis": {"type": "value", "name": "expectancy (R)"},
                "series": [{"type": "bar",
                            # per-bar colour is a plain JSON itemStyle property, not a JS
                            # callback -- safe, unlike the removed tooltip formatter above
                            "data": [{"value": round(c["expR"], 3),
                                      "itemStyle": {"color": "#16a34a" if c["expR"] > 0
                                                   else "#dc2626"}}
                                     for c in cal]}],
                "grid": {"left": 55, "right": 20, "top": 20, "bottom": 55},
            }).classes("w-full h-56").tooltip(
                "Each bar = one confidence band's realized expectancy (R) over broker-"
                "executed trades. A useful confidence signal should show bars rising "
                "left-to-right -- flat or inverted bars mean the LLM's confidence number "
                "isn't actually earning its place as a filter.")
        else:
            ui.label("No closed, broker-executed trades with confidence data yet.")\
                .classes("text-sm text-grey")

    # monthly attribution: where did the P&L actually come from
    with ui.expansion("Monthly attribution (USD)", icon="calendar_month",
                      caption="trend strategy vs. sleeve vs. other, by month")\
            .classes("w-full"):
        attrib = _monthly_attribution()
        if attrib:
            rows = [{"month": a["month"],
                     "trend": f"{a['trend']:+,.0f}", "sleeve": f"{a['sleeve']:+,.0f}",
                     "other": f"{a['other']:+,.0f}" if a["other"] is not None else "—",
                     "total": f"{a['total']:+,.0f}" if a["total"] is not None else "—"}
                    for a in reversed(attrib)]   # most recent month first
            ui.table(rows=rows,
                     columns=[{"name": "month", "label": "month", "field": "month", "align": "left"},
                              {"name": "trend", "label": "trend (USD)", "field": "trend", "align": "right"},
                              {"name": "sleeve", "label": "sleeve (USD)", "field": "sleeve", "align": "right"},
                              {"name": "other", "label": "other (USD)", "field": "other", "align": "right"},
                              {"name": "total", "label": "total (USD)", "field": "total", "align": "right"}])\
                .classes("w-full").props("dense")\
                .tooltip("'other' is cash interest + an untracked residual -- not separately "
                         "modeled, not an error")
        else:
            ui.label("No closed trades / equity history yet — attribution appears once trades "
                     "settle.").classes("text-sm text-grey")

    # constraint scorecard
    with ui.expansion("Constraint scorecard", icon="rule",
                      caption="how often each gate blocked a candidate")\
            .classes("w-full"):
        ui.button("Reset", icon="restart_alt", on_click=_reset_scorecard)\
            .props("flat dense size=sm")\
            .tooltip("Archives the current tally (nothing lost) and starts the "
                     "scorecard at zero. Does NOT touch open positions or trade history.")
        counts = journal.rejection_counts()
        if counts:
            rows = [{"constraint": reason, "blocked": n} for reason, n in counts]
            ui.table(rows=rows,
                     columns=[{"name": "constraint", "label": "constraint (gate)",
                               "field": "constraint", "align": "left"},
                              {"name": "blocked", "label": "times blocked",
                               "field": "blocked", "align": "right",
                               "sortable": True}])\
                .classes("w-full").props("dense")
        else:
            ui.label("No rejected candidates recorded yet. Once board scans run, "
                     "every blocked BUY/SELL is tallied here by gate.")\
                .classes("text-sm text-grey")

    # ADDED 2026-07-14: recent notable events (new orders, closes, DD-halts, reconcile
    # mismatches, orphaned-order cancellations) -- previously this history only existed in
    # the raw log file or HANDOFF.md, both external to the dashboard itself. Same source
    # (core/notable_events.py) that also feeds the Telegram alerts, so this view and any
    # alert you got should always agree.
    from dashboard.core import notable_events
    events = notable_events.recent(limit=20)
    with ui.expansion(f"Recent notable events ({len(events)})", icon="history",
                      caption="new orders, closes, DD-halts, reconcile mismatches")\
            .classes("w-full")\
            .tooltip("the same events that trigger a Telegram alert if configured"):
        if events:
            rows = [{"ts": e["ts"][:19].replace("T", " "), "level": e["level"],
                     "message": e["message"]} for e in events]
            ui.table(rows=rows,
                     columns=[{"name": "ts", "label": "when (UTC)", "field": "ts",
                               "align": "left"},
                              {"name": "level", "label": "level", "field": "level",
                               "align": "left"},
                              {"name": "message", "label": "event", "field": "message",
                               "align": "left"}])\
                .classes("w-full").props("dense")
        else:
            ui.label("No notable events recorded yet this instance.")\
                .classes("text-sm text-grey")


# connection_panel removed -- access points are switched manually; the header
# already shows the live access point + ping. (See git history / link_monitor
# for the ap-comparison table if you want it back.)


def _refresh_for_all_clients(*refreshables) -> None:
    # FIXED 2026-08-21: module-level (globally shared, not per-client) @ui.refreshable
    # panels' .refresh() is fine when called from WITHIN an already-active client's own
    # page render, but several call sites ALSO invoke it from the GLOBAL background tick
    # loop (_do_cheap()/_do_llm()), which runs OUTSIDE any specific browser client's
    # context. A refreshable's target DOM lives in whichever client last rendered it --
    # calling .refresh() with no active client context (or a STALE one, once that client
    # disconnects) hit NiceGUI's own "Client has been deleted but is still being used"
    # error on every cycle -- confirmed live as the actual cause of the dashboard's own
    # web server becoming unresponsive.
    #
    # FIRST fix (earlier the same day) only wrapped _refresh_all_panels() itself and
    # missed _do_llm()'s separate early-return path -- fixed by centralizing every
    # background-loop call site onto this one function.
    #
    # SECOND fix (later the same day): even after centralizing, the crash kept recurring
    # -- confirmed live: has_socket_connection can be True at the top of this loop and
    # still go stale by the time a client's actual .refresh() runs a few refreshables
    # later (a real race, not a logic error -- the client can disconnect at any point
    # during this synchronous-looking loop, since NiceGUI's own disconnect handling runs
    # concurrently). Pre-checking a flag before acting on it doesn't survive that gap.
    # Wrapping the ACTUAL refresh call defensively -- catch, log at debug (this is
    # routine, not a bug, once a tab closes mid-cycle), move on -- is correct regardless
    # of exactly which internal flag NiceGUI uses or when it flips.
    from nicegui import Client
    for client in list(Client.instances.values()):
        if not client.has_socket_connection:
            continue
        try:
            with client:
                for r in refreshables:
                    r.refresh()
        except Exception as e:                             # noqa: BLE001
            from dashboard.core.log import log
            log.debug("app: skipped a panel refresh for a client that disconnected "
                     "mid-cycle: %s", e)


def _refresh_all_panels() -> None:
    _refresh_for_all_clients(header_status, health_banner, macro_banner, opportunities,
                             grid, paper_panel, active_panel, gate_panel,
                             retrospective_panel, portfolio_panel, bell_button,
                             alerts_panel)


# ---- refresh orchestration -------------------------------------------------

async def _do_cheap() -> None:
    await run.io_bound(service.refresh_cheap)
    _refresh_all_panels()


async def _do_llm(force: bool = False) -> None:
    # `force` (manual refresh) overrides the weekend auto-pause -- an explicit
    # user click should always be honoured (budget permitting).
    if not force and SETTINGS["auto_pause"] and not _market_open():
        service.STATE["last_status"] = "market closed (auto-pause) — LLM skipped"
        _refresh_for_all_clients(header_status)
        return
    await run.io_bound(service.refresh_llm, SETTINGS["cap"])
    _refresh_all_panels()


_TICK_TIMEOUT_SEC = 120   # Defensive ceiling (2026-07-12): if ANY await inside _tick() ever
                          # hangs (a blocking IB/network call with no timeout of its own --
                          # account_summary()'s internal 10s timeout doesn't cover a hang
                          # elsewhere in the ib_insync event loop), the coroutine would never
                          # resume, so `finally: _busy["flag"]=False` would never run,
                          # permanently blocking every future tick with zero log output. Kept
                          # as defense-in-depth even though the ACTUAL dormancy found the same
                          # day (see app.on_startup below) turned out to have a different,
                          # more fundamental cause.


async def _tick() -> None:
    if _busy["flag"]:
        return
    _busy["flag"] = True
    # ADDED 2026-07-14: wall-clock duration of the WHOLE cycle (not just cheap/llm when they
    # actually run), stored for the new system-health banner -- the closest measurable proxy
    # this server-side process has for "is something making the system sluggish," after this
    # session found a real regression (a fix that added per-card broker round-trips) that
    # made response times jump from ~2s to 5-8s with no visible error, only caught because a
    # human happened to time a curl request. This surfaces that signal continuously instead.
    _t0 = dt.datetime.now()
    try:
        async def _do_tick_work():
            now = dt.datetime.now()
            last_cheap = service.STATE["last_cheap"]
            if last_cheap is None or (now - last_cheap).total_seconds() >= SETTINGS["cheap_min"] * 60:
                await run.io_bound(service.refresh_news)
                await _do_cheap()
            last_llm = service.STATE["last_llm"]
            if last_llm is None or (now - last_llm).total_seconds() >= SETTINGS["llm_min"] * 60:
                await _do_llm()
        await asyncio.wait_for(_do_tick_work(), timeout=_TICK_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        from dashboard.core.log import log
        log.error("_tick(): hung for >%ds, aborting this cycle so the next one isn't "
                 "permanently blocked (this does NOT cancel whatever thread-pool call was "
                 "actually stuck -- it may still be running in the background)",
                 _TICK_TIMEOUT_SEC)
    finally:
        service.STATE["last_tick_duration_sec"] = (dt.datetime.now() - _t0).total_seconds()
        service.STATE["last_tick_ts"] = dt.datetime.now()
        _busy["flag"] = False


async def _tick_loop() -> None:
    """FOUND 2026-07-12: _tick() was previously scheduled ONLY via `ui.timer(30.0, _tick)`
    called inside the per-client `@ui.page('/')` render function -- meaning the ENTIRE
    automated trading/monitoring loop (signal generation, order placement, DD_HALT checks,
    broker reconciliation, sleeve entries) ran ONLY while at least one browser client was
    connected to the page, and stopped COMPLETELY and SILENTLY the moment the last client
    disconnected -- with the web server still responding HTTP 200 to page loads throughout,
    giving zero indication anything was wrong. Confirmed directly: after a restart, both
    dashboards sat completely dormant for 20+ minutes across two separate restart cycles
    (zero log output at all -- not even a failed-attempt message); the INSTANT a browser tab
    was opened, a cheap refresh fired and the sleeve's staged-rollout clock -- stuck at None
    the whole time -- started immediately. For a system meant to run unattended with real
    money, "silently stops trading and monitoring whenever nobody has a tab open" is a
    serious reliability gap, not a cosmetic one.

    This runs `_tick()` from an `app.on_startup` background task instead -- entirely
    independent of whether any browser client is ever connected. The "never die from one
    call's failure" property (LATENT RECURRENCE GUARD: _tick() only catches
    asyncio.TimeoutError internally, so any OTHER unhandled exception from _do_cheap()/
    _do_llm() would otherwise silently kill this whole background task forever, recreating
    the exact same class of invisible dormancy via a different trigger) is implemented in
    `core/resilient_loop.run_forever()`, a small pure function with its own regression test
    (test_resilient_loop.py) -- this file can't be imported in a test itself (`ui.run()` at
    module level blocks), so the safety-critical logic lives there instead."""
    await asyncio.sleep(1.0)      # let the rest of app startup finish first
    from dashboard.core.resilient_loop import run_forever
    from dashboard.core.log import log

    def _on_error(e: BaseException) -> None:
        log.exception("_tick_loop(): unhandled exception in a tick -- logging and "
                      "continuing (this loop must never die): %s", e)
    await run_forever(_tick, 30.0, on_error=_on_error)


app.on_startup(lambda: asyncio.create_task(_tick_loop()))


@app.middleware("http")
async def _log_access(request, call_next):
    """ADDED 2026-07-14: logs every HTTP request (client IP, method, path, status,
    user-agent) to logs/access.log -- added when quant.carsonng.com's Cloudflare Access
    login gate was removed to make it public. This is the compensating visibility control,
    since without Access there's no built-in per-request identity log anymore, and this
    runs entirely locally rather than depending on a paid Cloudflare Logpush plan.

    IP resolution: behind a Cloudflare Tunnel, the real visitor IP arrives in the
    `CF-Connecting-IP` header (Cloudflare sets this on every proxied request, tunnel or
    not) -- the raw ASGI connection IP would just be cloudflared's own local process.
    Falls back to X-Forwarded-For (first hop) then the raw connection as a last resort
    for direct (non-Cloudflare) access, e.g. localhost during development."""
    from dashboard.core.log import access_log
    ip = (request.headers.get("cf-connecting-ip")
          or (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or None)
          or (request.client.host if request.client else "?"))
    response = await call_next(request)
    access_log.info("%s %s %s -> %s (UA: %s)", ip, request.method, request.url.path,
                    response.status_code, request.headers.get("user-agent", "?"))
    return response


async def _manual_refresh() -> None:
    if _busy["flag"]:
        ui.notify("Refresh already running…"); return
    _busy["flag"] = True
    ui.notify("Refreshing…")
    try:
        await run.io_bound(service.refresh_news)
        await _do_cheap()
        await _do_llm(force=True)
        ui.notify("Done. " + service.STATE["last_status"])
    finally:
        _busy["flag"] = False


async def _log_trades_now() -> None:
    """Manually turn the current signals into paper trades (no LLM call needed)."""
    from dashboard.core import paper
    logs = await run.io_bound(paper.place_from_state, service.STATE)
    placed = [l for l in logs if "PLACED" in l]
    paper_panel.refresh(); active_panel.refresh()
    ui.notify(f"Logged {len(placed)} paper trade(s).")


def _open_withdraw() -> None:
    """Manual cash-withdrawal helper: free funds from the CASH SHIELD (idle USD -> SGOV)
    first, NEVER the Core book, and earmark a reserve the sweep respects. The actual money
    transfer stays a manual IBKR action by design — this only prepares the cash."""
    from dashboard.execution import broker as _bk
    if not _bk.is_ib():
        ui.notify("Withdrawal helper is IBKR-only.", type="warning"); return
    with ui.dialog() as dlg, ui.card().classes("w-[92vw] max-w-[500px]"):
        ui.label("Withdraw cash — from SGOV / cash shield first, never Core").classes("text-lg font-bold")
        ui.label("Sells SGOV if idle cash is short and reserves the amount so the auto-sweep "
                 "won't re-buy it. Does NOT move money out — withdraw in IBKR manually, then "
                 "click Clear reserve.").classes("text-xs text-grey-7")
        amt = ui.number("Amount (USD)", value=5000, min=0, step=1000)\
            .props("dense outlined").classes("w-48")
        out = ui.label("").classes("text-sm font-mono whitespace-pre-wrap mt-1")

        async def _run_prep(dry: bool):
            a = float(amt.value or 0)
            if a <= 0:
                out.set_text("Enter an amount > 0"); return
            out.set_text("working…")
            res = await run.io_bound(_bk.prepare_withdrawal, a, dry)
            out.set_text(("✅ " if res.get("ready") else "⚠️ ") + str(res.get("log", "")))
            if not dry:
                portfolio_panel.refresh()

        async def _prep_dry():
            await _run_prep(True)

        async def _prep_real():
            await _run_prep(False)

        async def _clear():
            await run.io_bound(_bk.clear_withdraw_reserve)
            out.set_text("Reserve cleared (back to 0)."); portfolio_panel.refresh()

        with ui.row().classes("items-center gap-2 mt-2"):
            ui.button("Preview (dry-run)", icon="visibility", on_click=_prep_dry).props("flat")
            ui.button("Prepare (sell SGOV + reserve)", icon="savings", on_click=_prep_real)\
                .props("color=primary")\
                .tooltip("Sells SGOV to cover any shortfall and reserves the amount; "
                         "then withdraw it in IBKR and Clear reserve")
            ui.button("Clear reserve", icon="lock_open", on_click=_clear).props("flat")
        ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()


def _find_unrecorded_jump() -> tuple[int, float] | None:
    """(ts, amount) of the largest equity jump not already covered by a recorded cash flow,
    or None. Backstop for Layer 1 (service.detect_external_cash_flow) -- used to pre-fill the
    Cash flows dialog so a missed deposit can be corrected without hand-editing SQLite.
    Deliberately compares against NetLiq deltas (not the cash signature) because legacy
    history entries predating 2026-07-27 carry no cash/GPV to work from."""
    from dashboard.core import store
    hist, _ = store.cache_get("equity_history")
    flows, _ = store.cache_get("cash_flows")
    hist = hist or []
    if len(hist) < 2:
        return None
    covered = [f[0] for f in (flows or [])]
    best = None
    for i in range(1, len(hist)):
        ts, delta = hist[i][0], hist[i][1] - hist[i - 1][1]
        # a flow recorded within 10min of the jump already accounts for it
        if any(abs(ts - c) < 600 for c in covered):
            continue
        # only material moves are candidates -- same spirit as service.CASH_FLOW_MIN_*
        if abs(delta) < max(1000.0, abs(hist[i][1]) * 0.02):
            continue
        if best is None or abs(delta) > abs(best[1]):
            best = (ts, delta)
    return best


def _open_cash_flows() -> None:
    """ADDED 2026-07-27 (Layer 3): record a deposit/withdrawal by hand. Layers 1 and 2 should
    make this unnecessary, but on 2026-07-27 a real HKD 30,000 deposit WAS missed and the only
    way to correct it was editing SQLite directly -- which is not something this dashboard
    should ever require. Deposits are not trading profit; anything recorded here is netted out
    of Total P&L, the equity chart, and the drawdown-from-peak baseline."""
    from dashboard.core import store, paper
    acct = service.STATE.get("account") or {}
    ccy = acct.get("_ccy", "")

    with ui.dialog() as dlg, ui.card().classes("w-[92vw] max-w-[560px] gap-2"):
        ui.label("Cash flows — deposits & withdrawals").classes("text-lg font-bold")
        ui.label("Money moving IN or OUT of the account is not trading profit. Anything "
                 "recorded here is excluded from Total P&L, the equity chart and the "
                 "drawdown baseline.").classes("text-xs text-grey-7")

        flows, _ = store.cache_get("cash_flows")
        flows = list(flows or [])
        # ADDED 2026-08-18 (user-requested): the list below only ever showed individual
        # entries -- no running total, so "how much have I put in, net" required manually
        # summing every row by hand. Grouped by currency (defensive -- in practice every
        # entry uses the account's own base ccy, since the Amount field above isn't
        # currency-selectable, but a stray hand-typed value shouldn't silently misreport).
        net_lbl = ui.label("").classes("text-base font-bold")

        def _render_totals(cur: list) -> None:
            if not cur:
                net_lbl.set_text("Net deposits: nothing recorded yet")
                net_lbl.classes(replace="text-base font-bold text-grey-6")
                return
            by_ccy: dict[str, float] = {}
            for f in cur:
                by_ccy[f[2]] = by_ccy.get(f[2], 0.0) + f[1]
            text = "Net deposits: " + "  ·  ".join(
                f"{amt:+,.2f} {c}" for c, amt in by_ccy.items())
            net_lbl.set_text(text)
            total = sum(by_ccy.values())
            net_lbl.classes(replace="text-base font-bold " +
                            ("text-green" if total > 0 else
                             "text-red" if total < 0 else "text-grey-6"))

        body = ui.column().classes("w-full gap-1")

        def _render_list() -> None:
            body.clear()
            cur, _ = store.cache_get("cash_flows")
            _render_totals(cur or [])
            with body:
                if not cur:
                    ui.label("Nothing recorded yet.").classes("text-sm text-grey-6")
                    return
                for f in sorted(cur, key=lambda x: x[0]):
                    with ui.row().classes("items-center gap-2 w-full"):
                        ui.label(dt.datetime.fromtimestamp(f[0], tz=dt.timezone.utc).astimezone(HKT).strftime("%Y-%m-%d %H:%M") + " HKT")\
                            .classes("text-xs text-grey-7 font-mono")
                        ui.label(f"{f[1]:+,.2f} {f[2]}").classes(
                            "text-sm font-bold " + ("text-green" if f[1] > 0 else "text-red"))
                        ui.space()
                        ui.button(icon="delete", on_click=lambda ff=f: _delete(ff))\
                            .props("flat dense round size=sm").tooltip("remove this record")

        def _persist(new_flows: list) -> None:
            store.cache_set("cash_flows", sorted(new_flows, key=lambda x: x[0])[-500:])
            _render_list()
            portfolio_panel.refresh(); health_banner.refresh(); retrospective_panel.refresh()

        def _delete(f) -> None:
            cur, _ = store.cache_get("cash_flows")
            _persist([x for x in (cur or []) if not (x[0] == f[0] and x[1] == f[1])])
            ui.notify(f"Removed {f[1]:+,.2f} {f[2]}")

        ui.separator()
        ui.label("Recorded").classes("text-xs uppercase text-grey-6")
        _render_list()

        ui.separator()
        ui.label("Add").classes("text-xs uppercase text-grey-6")
        amt = ui.number(f"Amount ({ccy})", value=0, step=1000).props("dense outlined")\
            .classes("w-full")\
            .tooltip("positive = deposit into the account, negative = withdrawal out of it")
        when = ui.input("When (YYYY-MM-DD HH:MM)",
                        value=dt.datetime.now(HKT).strftime("%Y-%m-%d %H:%M"))\
            .props("dense outlined").classes("w-full")\
            .tooltip("Should match when the money actually landed, so the equity chart and "
                     "drawdown baseline are corrected from that point onward -- not just today")
        hint = ui.label("").classes("text-xs text-orange")

        def _detect() -> None:
            found = _find_unrecorded_jump()
            if not found:
                hint.set_text("No unrecorded jump found in the tracked history.")
                return
            ts, delta = found
            amt.set_value(round(delta, 2))
            when.set_value(dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(HKT).strftime("%Y-%m-%d %H:%M"))
            hint.set_text(f"Found an unexplained {delta:+,.0f} {ccy} move — check the amount "
                          "against your bank/IBKR record before saving (this is the NetLiq "
                          "delta, so it may include a little market drift).")

        def _add() -> None:
            try:
                ts = int(dt.datetime.strptime(when.value.strip(), "%Y-%m-%d %H:%M").timestamp())
            except ValueError:
                ui.notify("Date must look like 2026-07-27 13:11", type="warning"); return
            if not amt.value:
                ui.notify("Amount can't be zero.", type="warning"); return
            cur, _ = store.cache_get("cash_flows")
            _persist(list(cur or []) + [[ts, round(float(amt.value), 2), ccy]])
            ui.notify(f"Recorded {float(amt.value):+,.2f} {ccy}")
            amt.set_value(0); hint.set_text("")

        with ui.row().classes("items-center gap-2 w-full"):
            ui.button("Detect from history", icon="search", on_click=_detect).props("flat")\
                .tooltip("Scan the equity history for a large move not already recorded here")
            ui.space()
            ui.button("Add", icon="add", on_click=_add).props("color=primary")
            ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()


def _kill_and_relaunch_gateway() -> None:
    """Force-kill a stuck IB Gateway process tree and relaunch it hidden via IBC.
    Needed because a gateway that timed out mid-2FA can sit alive but unauthenticated
    forever (java.exe never exits) -- the port-down watchdog alone can't recover from
    that, only from a genuinely dead process (see HANDOFF 2026-07-08 "stuck alive" fix).
    Mirrors dashboard.ps1's own stale-gateway kill block, which only runs at task
    START -- this makes the same recovery available on demand from the UI."""
    import subprocess
    ibc_dir = r"C:\IBC-Live" if DASH_MODE == "live" else r"C:\IBC"
    # Distinguishing java.exe command-line substring for THIS mode's gateway -- "IBC-Live" for
    # live, "IBC\config.ini" (one backslash) for paper, which does NOT match "IBC-Live\..." (no
    # regex needed -- -like's wildcard matching treats \ and . as plain literal characters).
    gw_match = "*IBC-Live*" if DASH_MODE == "live" else "*IBC\\config.ini*"
    # NOTE: Stop-Process -Force silently fails ("Access is denied") against this Gateway
    # process -- it runs at a higher integrity/token level than this subprocess's context,
    # and -ErrorAction SilentlyContinue swallowed the failure (found 2026-07-09: the
    # watchdog's identical Stop-Process-based kill never actually worked, it just kept
    # spawning duplicate gateway instances). WMI's Win32_Process.Terminate() uses a
    # different privilege path and empirically works where Stop-Process doesn't.
    # ALSO match by COMMAND LINE, not window title -- the title changes throughout login
    # (Login dialog -> "Authenticating..." -> "Second Factor Authentication" -> only
    # eventually "IBKR Gateway" once fully connected), so a process stuck mid-login was
    # completely invisible to the old title-only match (found live, 2026-07-09: a process
    # sat stuck at "Authenticating..." for 10+ minutes, untouched by repeated kill attempts).
    ps = (
        "function Kill-Hard($id) { try { "
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$id\" -ErrorAction Stop; "
        "if ($p) { Invoke-CimMethod -InputObject $p -MethodName Terminate -ErrorAction Stop | Out-Null } "
        "} catch {} }; "
        "Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -match 'StartGateway' } | "
        "ForEach-Object { Kill-Hard $_.ProcessId }; "
        "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.CommandLine -like '{gw_match}' }} | "
        "ForEach-Object { Kill-Hard $_.ProcessId }; "
        "Start-Sleep -Seconds 2; "
        f"Start-Process -FilePath 'wscript.exe' -ArgumentList '//B','//Nologo',"
        f"'{ibc_dir}\\start_hidden.vbs' -WindowStyle Hidden"
    )
    from dashboard.core.log import log
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                          creationflags=subprocess.CREATE_NO_WINDOW)
        log.info("gateway kill+relaunch triggered (mode=%s, ibc=%s)", DASH_MODE, ibc_dir)
    except Exception:
        log.exception("gateway kill+relaunch failed")


def _restart_server() -> None:
    """Exit the process so the watchdog (DashboardApp task / dashboard.ps1)
    relaunches it fresh with the latest code, ~10s later. If the IB Gateway link
    is currently down, also force-kill + relaunch it -- restarting only the app
    left a stuck gateway untouched, so "Restart" silently didn't fix the thing
    the user was actually restarting for."""
    import os
    import threading
    from dashboard.core.log import log
    from dashboard.execution import broker as _bk

    gw_kicked = False
    if _bk.is_ib():
        bc = service.STATE.get("broker_conn") or {}
        # NOTE: gate on "available" (link up) only -- "ok" means "is a paper acct",
        # which is EXPECTED False on the live dashboard even when healthy (green/
        # orange/red header dot: available+ok=green paper, available-only=orange
        # healthy-live, unavailable=red). Gating on "ok" would kill a fine live
        # gateway on every single restart click.
        if not bc.get("available"):
            _kill_and_relaunch_gateway()
            gw_kicked = True

    msg = "Restarting app"
    if gw_kicked:
        msg += " + IB Gateway (was down — forcing a fresh relaunch/login)"
    msg += " — app is back in ~10s"
    if gw_kicked:
        msg += ", gateway login can take ~30-60s (+2FA if prompted)"
    msg += ". Reload the page shortly."
    ui.notify(msg, type="warning", timeout=9000)
    log.info("restart requested from UI; exiting for watchdog relaunch%s",
              " (+ gateway kill/relaunch)" if gw_kicked else "")
    threading.Timer(1.2, lambda: os._exit(0)).start()


def _confirm_restart() -> None:
    """2026-07-23: the Restart button used to call _restart_server() directly, with zero
    confirmation, sitting in the same row and visual weight as routine buttons (Manual
    refresh, Log trades now) -- a misclick force-kills the live real-money process (and
    the IB Gateway too, if it's currently down). Same dialog pattern as _open_withdraw()."""
    from dashboard.execution import broker as _bk
    bc = service.STATE.get("broker_conn") or {}
    gw_would_kick = _bk.is_ib() and not bc.get("available")
    with ui.dialog() as dlg, ui.card().classes("w-[92vw] max-w-[440px]"):
        ui.label("Restart the app now?").classes("text-lg font-bold")
        msg = ("Exits the live process immediately -- the watchdog relaunches it fresh in "
              "~10s.")
        if gw_would_kick:
            msg += (" The IB Gateway link is currently down, so this will ALSO force-kill "
                   "and relaunch it (~30-60s + 2FA if prompted).")
        ui.label(msg).classes("text-sm text-grey-7")
        with ui.row().classes("justify-end gap-2 w-full mt-2"):
            ui.button("Cancel", on_click=dlg.close).props("flat")
            def _confirmed():
                dlg.close()
                _restart_server()
            ui.button("Restart now", icon="restart_alt", on_click=_confirmed)\
                .props("color=negative")
    dlg.open()


async def _archive_records(table) -> None:
    """Archive the rows ticked in a paper-trades table (specific records)."""
    from dashboard.core import paper
    ids = [r["id"] for r in table.selected]
    if not ids:
        ui.notify("Select one or more rows first."); return
    n = await run.io_bound(paper.archive_trades, ids)
    paper_panel.refresh(); active_panel.refresh(); retrospective_panel.refresh()
    ui.notify(f"Archived {n} record(s). Restore them via View archive.")


async def _export_results() -> None:
    from dashboard.web import report
    from dashboard.execution import broker as _bk
    csvp, repp = await run.io_bound(report.export)
    rep = report.build_report()
    _title = "Live-trade report (copy to share)" if _bk.is_live() else "Paper-trade report (copy to share)"
    with ui.dialog() as dlg, ui.card().classes("w-[92vw] max-w-[680px]"):
        ui.label(_title).classes("text-lg font-bold")
        ui.label(f"Saved: {repp}").classes("text-xs text-grey")
        ui.label(f"CSV:   {csvp}").classes("text-xs text-grey")
        ui.code(rep).classes("w-full max-h-[60vh] overflow-auto")
        ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()
    ui.notify("Exported report + CSV to exports/")


async def _export_retrospective() -> None:
    from dashboard.web import retrospective
    from dashboard.execution import broker as _bk
    path = await run.io_bound(retrospective.export)
    rep = retrospective.build()
    _title = "Live-trading retrospective" if _bk.is_live() else "Forward-test retrospective"
    with ui.dialog() as dlg, ui.card().classes("w-[92vw] max-w-[680px]"):
        ui.label(_title).classes("text-lg font-bold")
        ui.label(f"Saved: {path}").classes("text-xs text-grey")
        ui.code(rep).classes("w-full max-h-[60vh] overflow-auto")
        ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()
    ui.notify("Exported retrospective to exports/")


def _open_archive() -> None:
    from dashboard.core import paper
    raw = paper.archived_trades()
    rows = [{"rowid": t["rowid"], "batch": _fmt_ts(t["archive_batch"]),
             "instrument": t["instrument"], "dir": t["direction"], "method": t["method"],
             "status": t["status"], "R": round(t["realized_r"], 2),
             "opened": _fmt_ts(t["ts"]), "closed": _fmt_ts(t["exit_ts"])} for t in raw]
    with ui.dialog().props("full-width") as dlg, ui.card().classes("w-full"):
        ui.label(f"Archived trades ({len(rows)})").classes("text-lg font-bold")
        if not rows:
            ui.label("No archived trades yet.").classes("text-sm text-grey")
        else:
            cols = [{"name": c, "label": c, "field": c, "sortable": True}
                    for c in ["batch", "instrument", "dir", "method", "status",
                              "R", "opened", "closed"]]
            table = ui.table(columns=cols, rows=rows, row_key="rowid",
                             selection="multiple").classes("w-full").props("dense")
            ui.label("Tick rows, then Unarchive to move them back to the live journal.")\
                .classes("text-xs text-grey-6")

            async def _unarch() -> None:
                ids = [r["rowid"] for r in table.selected]
                if not ids:
                    ui.notify("Select one or more rows first."); return
                n = await run.io_bound(paper.unarchive, ids)
                paper_panel.refresh(); active_panel.refresh()
                dlg.close()
                ui.notify(f"Unarchived {n} trade(s) back to the live journal.")

            ui.button("Unarchive selected", icon="unarchive", on_click=_unarch).props("color=primary")
        ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()


async def _reset_scorecard() -> None:
    """Archive + clear the Constraint-scorecard log (rejected_signals). Purely an
    audit/display log -- does NOT touch paper_trades/ib_mirror, so open positions
    and trade history are completely unaffected."""
    from dashboard.core import journal
    with ui.dialog() as dlg, ui.card():
        ui.label("Reset constraint scorecard?").classes("text-lg font-bold")
        ui.label("Archives the current tally, then starts the scorecard at zero. "
                 "Nothing is deleted — query rejected_signals_archive to see prior "
                 "counts. Open positions and trade history are untouched; new "
                 "rejections keep being tallied as board scans run.").classes("text-sm")
        with ui.row():
            ui.button("Cancel", on_click=dlg.close).props("flat")
            async def _go():
                dlg.close()
                r = await run.io_bound(journal.archive_and_reset_rejections)
                retrospective_panel.refresh()
                ui.notify(f"Archived {r['archived']} record(s) as {r['batch']}. "
                          f"Scorecard reset.")
            ui.button("Reset", on_click=_go).props("color=negative")
    dlg.open()


async def _archive_reset() -> None:
    from dashboard.core import paper
    with ui.dialog() as dlg, ui.card():
        ui.label("Archive & reset journal?").classes("text-lg font-bold")
        ui.label("Saves a snapshot (CSV + report) and copies all trades to the "
                 "archive, then clears the live journal so counting restarts at 0. "
                 "Nothing is deleted — archived trades are kept.").classes("text-sm")
        with ui.row():
            ui.button("Cancel", on_click=dlg.close).props("flat")
            async def _go():
                dlg.close()
                r = await run.io_bound(paper.archive_and_reset)
                paper_panel.refresh(); active_panel.refresh(); header_status.refresh()
                ui.notify(f"Archived {r['archived']} trade(s) as {r['batch']}. "
                          f"Journal reset.")
            ui.button("Archive & reset", on_click=_go).props("color=negative")
    dlg.open()


def _open_info_modal() -> None:
    """2026-07-24: the head of the page used to stack the mode-explainer sentence,
    clock_row(), and header_status()'s data-source/timestamp/LLM-budget/account-P&L lines
    on every visit -- five separate blocks of small text before reaching any actual control.
    None of it is the "is my money okay" signal (that's the broker-connection line still in
    header_status() plus health_banner(), both still inline) -- it's all useful occasionally,
    not on every single visit. Moved here, rebuilt fresh from STATE each time it opens (not
    refreshable -- it's only ever open a few seconds at a time)."""
    _live = os.environ.get("IB_ALLOW_LIVE", "").lower() in ("1", "true", "yes")
    from dashboard.execution import broker as _bk
    with ui.dialog() as dlg, ui.card().classes("w-[92vw] max-w-[520px] gap-2"):
        ui.label("Session info").classes("text-lg font-bold")

        if _live:
            ui.label("Auto-trades qualifying signals with REAL MONEY on this account. "
                     "Not a suggestion box — verify positions directly in IBKR too.")\
                .classes("text-sm text-red-6 font-bold")
        else:
            ui.label("Auto-trades qualifying signals on the IBKR paper account "
                     "(simulated fills, no real money).")\
                .classes("text-sm text-grey-7")
            # ADDED 2026-08-06: fuller disclaimer, paper only (see the short always-visible
            # version on the main page for why placement matters -- this is the supplementary
            # detail for anyone who opens this modal, not the primary carrier of the point).
            with ui.column().classes("gap-1 bg-orange-1 rounded p-2"):
                ui.label("Disclaimer").classes("text-xs font-bold text-orange-9")
                ui.label(
                    "Hypothetical/simulated performance has inherent limitations and differs "
                    "from real trading in ways impossible to fully account for (real "
                    "liquidity, execution slippage, behavioral factors). No representation is "
                    "made that any account will achieve similar results. This is a personal "
                    "research project, not operated by a licensed investment adviser or "
                    "broker-dealer; its logic is automated, in part LLM-assisted, and may "
                    "contain errors or bugs. Provided as-is, without warranty of accuracy. "
                    "Past performance, real or simulated, does not indicate future results.")\
                    .classes("text-xs text-orange-8")

        ui.separator()
        clock_row()

        ui.separator()
        data_txt, data_css = _data_source_text()
        ui.label(data_txt).classes("text-sm " + data_css)
        ui.label("Prices/scores: " + _ago(service.STATE["last_cheap"])).classes("text-sm text-grey-7")
        ui.label("LLM scan: " + _ago(service.STATE["last_llm"])).classes("text-sm text-grey-7")

        cap = SETTINGS["cap"]
        used = service.STATE.get("calls_today", 0)
        near = used >= cap - 10
        ui.label(f"API calls today: {used}/{cap}").classes(
            "text-sm " + ("text-red font-bold" if near else "text-grey-7"))
        shared_used = service.STATE.get("shared_calls_today", 0)
        shared_near = shared_used >= cap - 10
        shared_by_project = service.STATE.get("shared_calls_by_project", {})
        ui.label(f"Shared quota (quant+study+events): {shared_used}/{cap}").classes(
            "text-sm " + ("text-red font-bold" if shared_near else "text-grey-7")
        ).tooltip(", ".join(f"{k}: {v}" for k, v in shared_by_project.items()) or "no data yet")

        if _bk.is_ib():
            acct = service.STATE.get("account") or {}
            if acct:
                cc = acct.get("_ccy", "")
                nl = acct.get("NetLiquidation"); cash = acct.get("TotalCashValue")
                bp = acct.get("BuyingPower"); upnl = acct.get("UnrealizedPnL")
                parts = []
                if nl is not None:   parts.append(f"NetLiq {cc} {nl:,.0f}")
                if cash is not None: parts.append(f"cash {cc} {cash:,.0f}")
                if bp is not None:   parts.append(f"BP {cc} {bp:,.0f}")
                if upnl:             parts.append(f"uPnL {cc} {upnl:+,.0f}")
                ui.label(" · ".join(parts)).classes(
                    "text-sm " + ("text-green" if (upnl or 0) >= 0 else "text-red"))\
                    .tooltip(f"{_bk.name()} account (base ccy): net liquidation, cash, "
                             "buying power, unrealized P&L -- also on the Board tab's "
                             "Portfolio panel")
        else:
            conn = service.STATE.get("conn")
            if conn:
                from dashboard.execution import link_monitor
                lk = link_monitor.status()
                ap = lk.get("access_point") or conn["server"]
                ping = lk.get("ping_ms") or conn["ping_ms"]
                best_ap, best_ping = lk.get("best_ap"), lk.get("best_ping")
                if best_ap and ap and best_ap != ap and best_ping and ping - best_ping > 15:
                    hint = (f"faster access point available: {best_ap} ~{best_ping:.0f}ms"
                            + ("" if lk.get("can_reroll") else " (pin via icon)"))
                    ui.label(hint).classes("text-sm text-orange")\
                        .tooltip("the link monitor re-rolls automatically when credentials "
                                 "are set, else pin it via the MT5 connection icon")

        ui.label(service.STATE["last_status"]).classes("text-sm text-grey-5 italic")

        with ui.row().classes("justify-end w-full mt-1"):
            ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()


# ---- ADDED 2026-08-26: in-app notification center (bell) -----------------------------------
# notable_events were previously visible only inside Retrospective's collapsed "Recent
# events" expansion -- you had to KNOW to go look. v2 (2026-08-26, user feedback: the
# dialog's open-everything-as-read behavior dismissed real alerts the user never saw):
# the bell is now just a POINTER -- it shows the true unread count (per-event read_ts in
# the changelog table) and clicking it navigates to the dedicated ALERTS TAB, where read/
# unread transitions happen only through explicit user actions (per-row check or a
# deliberate "Mark all as read" button). Nothing auto-marks anything.

def _unread_events_count() -> int:
    """Bell counts ONLY red-tier (needs attention) unread events -- worth-knowing/log
    never interrupt."""
    try:
        from dashboard.core import notable_events
        return notable_events.unread_count(tier="red")
    except Exception:                                  # noqa: BLE001 -- never break rendering
        return 0


@ui.refreshable
def bell_button() -> None:
    unread = _unread_events_count()
    btn = ui.button(icon="notifications", on_click=_goto_alerts)\
        .props(("flat dense round size=sm color=negative") if unread else
               "flat dense round size=sm")\
        .tooltip("Notable events" +
                 (f" — {unread} unread — click to review" if unread else " — all read"))\
        .classes("relative")
    if unread:
        # Button has no .badge() method in the installed NiceGUI version -- a badge is a
        # CHILD element of the button it decorates, not a method on it.
        with btn:
            ui.badge(str(min(unread, 99)) + ("+" if unread > 99 else ""),
                     color="red").props("floating")


def _goto_alerts() -> None:
    """Bell click: jump to the Alerts tab pre-filtered to NEEDS-ATTENTION only (never
    marks anything read)."""
    SETTINGS["alerts_filter"] = "attention"
    _save_settings()
    try:
        tabs = getattr(ui.context.client, "_tabs", None)
        if tabs is not None:
            tabs.set_value("alerts")
    except Exception as e:                             # noqa: BLE001 -- cosmetic only
        from dashboard.core.log import log
        log.debug("bell navigation failed: %s", e)
    alerts_panel.refresh()


ALERTS_FILTERS = {"all": "All", "attention": "Needs attention"}
_ALERTS_PAGE_SIZE = 50


@ui.refreshable
def alerts_panel() -> None:
    """Alerts v3 -- three tiers, grouped incidents, plain-language titles. Read/unread is
    per incident and changes ONLY via explicit user actions here."""
    from dashboard.core import notable_events

    # FIXED 2026-08-26: a PERSISTED alerts_filter can name an option that no longer exists
    # (confirmed live: "unread" survived in ui_settings after the filter options were
    # renamed to all/attention). ui.toggle raises ValueError on a value outside its options
    # dict, so a stale setting was a hard 500 on the whole page. Fall back to the default
    # rather than trusting whatever was persisted by an older build.
    filt = SETTINGS.get("alerts_filter", "all")
    if filt not in ALERTS_FILTERS:
        filt = "all"
        SETTINGS["alerts_filter"] = filt

    def _set_filter(e) -> None:
        SETTINGS["alerts_filter"] = e.value
        _save_settings()
        alerts_panel.refresh()

    events = notable_events.recent(300)
    reds = [ev for ev in events if ev["tier"] == "red"]
    yellows = [ev for ev in events if ev["tier"] == "yellow"]
    whites = [ev for ev in events if ev["tier"] == "white"]
    unread_red = notable_events.unread_count(tier="red")

    def _render_card(ev: dict, accent: str, bg: str) -> None:
        sym = ev.get("symbol")

        def _mark() -> None:
            notable_events.mark_read(ev["id"])
            alerts_panel.refresh()
            bell_button.refresh()

        def _open_trade() -> None:
            SETTINGS.update(trades_filter="closed" if "cancel" in (ev.get("kind") or "")
                            else "all", trades_search=sym or "")
            _save_settings()
            try:
                tabs = getattr(ui.context.client, "_tabs", None)
                if tabs is not None:
                    tabs.set_value("trades")
            except Exception:                          # noqa: BLE001
                pass
            paper_panel.refresh()

        cls = ("items-start gap-2 w-full p-2 rounded-b border-l-4 "
               f"{accent} {bg}" + ("" if ev["read"] else " font-medium"))
        with ui.row().classes(cls):
            with ui.column().classes("gap-0 grow min-w-[220px]"):
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.label(ev["title"]).classes(
                        "text-sm " + ("font-bold" if not ev["read"] else ""))
                    if (ev.get("count") or 1) > 1:
                        ui.badge(f"x{ev['count']}", color="grey-7").classes("text-xs")
                    if not ev["read"]:
                        ui.badge("UNREAD", color="blue").props("outline").classes("text-xs")
                    ui.label((ev.get("last_ts") or ev["ts"]).replace("T", " ")[:16])\
                        .classes("text-xs text-grey-6")
                if ev.get("detail"):
                    ui.label(ev["detail"]).classes("text-xs text-grey-7")
                with ui.expansion("details", caption="raw event text")\
                        .classes("text-xs text-grey-6").style("min-height:0"):
                    ui.label(ev["message"]).classes("text-xs font-mono whitespace-pre-wrap")
            with ui.row().classes("items-center gap-1 self-center"):
                if sym and ev.get("kind") in ("order-cancelled", "position-closed",
                                              "reconcile-mismatch"):
                    ui.button(icon="receipt_long", on_click=_open_trade)\
                        .props("flat dense round size=sm")\
                        .tooltip(f"Open Trades filtered to {sym}")
                if not ev["read"]:
                    ui.button(icon="check", on_click=_mark)\
                        .props("flat dense round size=sm").tooltip("Mark as read")

    with ui.row().classes("items-center justify-between w-full flex-wrap gap-2"):
        ui.label("Alerts").classes("text-lg font-bold")
        ui.badge(f"{unread_red} need attention",
                 color="red" if unread_red else "grey")
        if any(not ev["read"] for ev in events):
            ui.button("Mark all as read", icon="done_all",
                      on_click=lambda: (notable_events.mark_all_read(),
                                        alerts_panel.refresh(), bell_button.refresh()))\
                .props("flat dense")\
                .tooltip("Explicitly clears every unread flag. This is the ONLY "
                         "bulk way anything gets marked read.")
    with ui.row().classes("items-center gap-2 w-full flex-wrap"):
        ui.toggle(ALERTS_FILTERS, value=filt, on_change=_set_filter).props("dense")
        _sym_q = {"v": ""}

        def _set_sym(e) -> None:
            _sym_q["v"] = (e.value or "").strip().lower()
            alerts_panel.refresh()

        # FIXED 2026-08-26: `clearable=True` is not a ui.input kwarg in the installed NiceGUI
        # (3.12.1 -- ui.select/ui.toggle have it, ui.input does not), so this raised TypeError
        # and 500'd the whole page. Quasar's own `clearable` prop does the same job.
        ui.input(placeholder="Filter by symbol…", on_change=_set_sym)\
            .props("dense outlined clearable").classes("w-[180px]")
        ui.label("Only 🔴 needs-attention events push to Telegram/ntfy.")\
            .classes("text-xs text-grey-6 ml-auto")

    def _matches(ev: dict) -> bool:
        return not _sym_q["v"] or _sym_q["v"] in (ev.get("symbol") or "").lower() \
            or _sym_q["v"] in (ev["title"] or "").lower()

    shown = [ev for ev in events if _matches(ev)]
    reds_shown = [ev for ev in shown if ev["tier"] == "red"]
    yellows_shown = [ev for ev in shown if ev["tier"] == "yellow"]
    whites_shown = [ev for ev in shown if ev["tier"] == "white"]

    if filt == "attention":
        if reds_shown:
            with ui.column().classes("w-full gap-2 mt-2"):
                for ev in reds_shown:
                    _render_card(ev, "border-red-500", "bg-red-1")
        else:
            ui.label("Nothing needs attention.").classes("text-sm text-grey mt-2")
        return

    if reds_shown:
        ui.label("🔴 Needs attention").classes("text-sm font-bold mt-2 text-red")
        with ui.column().classes("w-full gap-2"):
            for ev in reds_shown:
                _render_card(ev, "border-red-500", "bg-red-1")
    if yellows_shown:
        ui.label("🟡 Worth knowing").classes("text-sm font-bold mt-3 text-orange-8")
        with ui.column().classes("w-full gap-2"):
            for ev in yellows_shown[:_ALERTS_PAGE_SIZE]:
                _render_card(ev, "border-orange-400", "")
    with ui.expansion(f"⚪ Log ({len(whites_shown)})", caption="bookkeeping events")\
            .classes("w-full mt-2"):
        with ui.column().classes("w-full gap-1"):
            for ev in whites_shown[:_ALERTS_PAGE_SIZE]:
                _render_card(ev, "border-grey-400", "")
    if not shown:
        ui.label("Nothing recorded yet.").classes("text-sm text-grey mt-2")
    elif not reds_shown and not yellows_shown and filt == "all":
        pass


# ---- page ------------------------------------------------------------------

@ui.page("/")
def main_page() -> None:
    service.restore_cache()
    _live = os.environ.get("IB_ALLOW_LIVE", "").lower() in ("1", "true", "yes")
    _density_gap = "gap-2" if SETTINGS.get("density")=="compact" else "gap-3"
    _density_pad = "p-2" if SETTINGS.get("density")=="compact" else "p-3"
    with ui.column().classes(f"w-full max-w-[1280px] mx-auto {_density_gap} p-2 md:p-4"):
        # B1 2026-08-26: header row now wraps on narrow screens instead of overflowing;
        # title steps down a size below md.
        with ui.row().classes("items-center gap-2 md:gap-3 w-full flex-wrap"):
            ui.label("Quantitative Trading System").classes(
                "text-xl md:text-2xl font-bold")
            ui.button(icon="info", on_click=_open_info_modal).props("flat dense round size=sm")\
                .tooltip("Session info: connection clocks, LLM budget, account detail")
            # B4 2026-08-26: notification center -- red badge while unread notable events exist
            bell_button()
            # Unmistakable mode badge so concurrent PAPER/LIVE windows are never confused.
            if _live:
                ui.badge("● LIVE — REAL MONEY", color="red").classes("text-sm px-3 py-1")
            else:
                ui.badge("● PAPER", color="green").classes("text-sm px-3 py-1")
            # Account PHASE (auto-switches by equity) x sleeve ENABLED (explicit opt-in, see
            # sleeve.py) -- BOTH must be true for the sleeve to genuinely be trading. Badge
            # text distinguishes "threshold reached, not built/enabled" from "actually active"
            # so it can never again claim something that isn't really running.
            try:
                from dashboard.core import paper as _pp
                from dashboard.core import sleeve as _sl
                _acct = service.STATE.get("account") or {}
                _nl = _acct.get("NetLiquidation")
                _eq_usd = (float(_nl) / 7.8) if _nl else None      # HKD->USD peg (display only)
                _ph = _pp.account_phase(_eq_usd)
                _sleeve_on = _sl.sleeve_enabled() and _pp.sleeve_active(_eq_usd)
                # FIXED 2026-07-23: PHASE2_NAV_USD was set to 0 on both launchers (removing
                # the sleeve's equity gate entirely, see run_dashboard_live.ps1) -- with a $0
                # threshold, account_phase() -> equity_usd >= 0 is true for any real balance,
                # so Phase 1 became UNREACHABLE (confirmed: every render this whole period has
                # shown "Phase 2 ...", never "Phase 1"). The badge kept branching on a phase
                # distinction that no longer exists, and its tooltip hardcoded "(~500K HKD)" --
                # the OLD threshold, directly contradicting the "$0" the same sentence computed
                # dynamically two words earlier. Both fixed: when the gate is actually disabled
                # (threshold <= 0), drop the phase language entirely and show just the one
                # distinction that's still real (sleeve active vs not); the tooltip's HKD figure
                # is now computed from PHASE2_NAV_USD, not hardcoded, so it can't drift from
                # whatever the threshold actually is if it's ever set nonzero again.
                _gate_active = _pp.PHASE2_NAV_USD > 0
                if _gate_active and _ph == 1:
                    _txt, _color = "Phase 1 · core-only", "blue"
                elif _sleeve_on:
                    _txt, _color = (("Phase 2 · sleeve ACTIVE" if _gate_active
                                     else "Sleeve ACTIVE"), "purple")
                else:
                    _txt, _color = ((("Phase 2 threshold · sleeve NOT enabled") if _gate_active
                                     else "Sleeve NOT enabled"), "grey")
                _threshold_txt = (
                    f"equity threshold ~USD {_pp.PHASE2_NAV_USD:,.0f} "
                    f"(~HKD {_pp.PHASE2_NAV_USD * 7.8:,.0f}); "
                    if _gate_active else
                    "no equity threshold currently (gate removed, PHASE2_NAV_USD=0); ")
                ui.badge(_txt, color=_color).classes("text-sm px-3 py-1")\
                    .tooltip(_threshold_txt +
                             "sleeve also needs SLEEVE_ENABLED=1 on this instance's launcher "
                             "to actually place orders (set on both paper and live launchers)")
            except Exception:                                      # never break the header
                pass
            # CONCURRENT paper+live: both processes run continuously, each on its own Cloudflare
            # hostname (same apex domain, real HTTPS, no reverse-proxy path/websocket rewriting
            # issues). This is a plain NAVIGATION link to the sibling instance -- NOT a mode-flip/
            # restart -- because both are already live and trading independently. Configurable via
            # PAPER_URL/LIVE_URL env (defaults match the Cloudflare tunnel's two hostnames).
            _other = "PAPER" if _live else "LIVE"
            _other_url = (os.environ.get("PAPER_URL", "https://quant.carsonng.com") if _other == "PAPER"
                          else os.environ.get("LIVE_URL", "https://quant-live.carsonng.com"))
            ui.link(f"⇄ Open {_other}", _other_url, new_tab=True)\
                .classes("text-sm px-3 py-1 rounded border "
                         + ("border-green-600 text-green-700" if _other == "PAPER"
                            else "border-red-600 text-red-700"))\
                .tooltip(f"opens the {_other} dashboard (separate always-on instance, own gateway "
                         "+ account + database; both trade concurrently)")
            # A1 2026-08-26: one-page health check across BOTH instances (phone-friendly)
            ui.link("Fleet status", "/fleet", new_tab=True)\
                .classes("text-sm px-3 py-1 rounded border border-grey-400 text-grey-7")\
                .tooltip("one page, both instances' live health at a glance "
                         "(also reachable directly at /status?fmt=html per instance)")
        # 2026-07-24: the static mode-explainer sentence and clock_row() used to render here
        # on every visit -- the sentence is redundant with the "● LIVE — REAL MONEY" /
        # "● PAPER" badge right above it, and the clocks are rarely the first thing anyone
        # needs. Both moved into the ⓘ info modal next to the title (_open_info_modal()).

        # ADDED 2026-08-06, PAPER ONLY (user-requested; live intentionally excluded): short
        # legal/informational disclaimer, ALWAYS visible (not behind the info-modal click) --
        # placement matters for a disclaimer to actually count: it needs to be unavoidable on
        # page load and sit near the numbers it's disclaiming, not buried in a footer (easy to
        # scroll past) or a tab (most visitors never click it). The fuller version lives in
        # _open_info_modal() for anyone who wants the detail; this line carries the actual
        # legal weight since it's the one nobody can miss.
        if not _live:
            ui.label("Paper trading — simulated fills, no real capital at risk. Personal "
                     "research project, not investment advice or a solicitation to buy or "
                     "sell any security.").classes("text-xs text-orange-8 bg-orange-1 rounded px-2 py-1")

        # 2026-07-23: this account-health block used to render BELOW the settings row --
        # meaning the thing you check on every visit (is my real money okay) sat under a
        # row of controls you touch once a month (LLM interval, risk%, overextended band).
        # Moved account health first; settings now follow it, right before the tabs.
        header_status()
        health_banner()

        # 2026-07-23: split ACTIONS (things you click to do something now) from SETTINGS
        # (set-once-and-forget toggles) -- these used to be one dense row with 5 labeled
        # input groups and 4 buttons, all always visible, right below the health status.
        # Actions stay visible (you might want Manual refresh/Restart without digging through
        # a collapsed panel); settings move into a collapsed-by-default expansion, since none
        # of them need re-checking on every visit the way account health does.
        # B1 2026-08-26: flex-wrap so the action buttons flow to a second line on a phone
        # instead of overflowing horizontally.
        with ui.row().classes("items-center gap-2 w-full flex-wrap"):
            ui.button("Manual refresh", icon="refresh", on_click=_manual_refresh).props("color=primary")
            ui.button("Log trades now", icon="playlist_add", on_click=_log_trades_now).props("flat")
            from dashboard.execution import broker as _bk_hdr
            if _bk_hdr.is_ib():
                ui.button("Withdraw", icon="savings", on_click=_open_withdraw).props("flat")\
                    .tooltip("Prepare a cash withdrawal from SGOV/cash shield first (never Core); "
                             "you still transfer the money manually in IBKR")
            ui.button("Cash flows", icon="account_balance", on_click=_open_cash_flows).props("flat")\
                .tooltip("Record a deposit/withdrawal by hand -- e.g. if a monthly contribution "
                         "wasn't auto-detected and is being shown as trading profit")
            ui.button("Restart", icon="restart_alt", on_click=_confirm_restart)\
                .props("flat color=negative")\
                .tooltip("exit the app so the watchdog relaunches it fresh (~10s); "
                         "if the IB Gateway link is down, also force-kills and "
                         "relaunches it (~30-60s + 2FA if prompted)")

        # P1 spec: Settings moved from inline expansion (pushed Board down) to dialog
        def _open_settings() -> None:
            with ui.dialog() as dlg, ui.card().classes("w-[92vw] max-w-[680px] max-h-[85vh] overflow-auto"):
                ui.label("Settings").classes("text-lg font-bold")
                ui.label("Changes persist across restarts.").classes("text-xs text-grey-6")
                ui.separator()
                # Row 1: LLM + Pause
                with ui.row().classes("items-center gap-4 w-full flex-wrap"):
                    ui.label("LLM scan:").classes("text-sm w-[90px]")
                    ui.toggle({15: "15m", 30: "30m", 60: "60m", 120: "2h", 240: "4h"},
                              value=SETTINGS["llm_min"],
                              on_change=lambda e: (SETTINGS.update(llm_min=e.value),
                                                   _save_settings())).props("dense")
                    ui.checkbox("Pause LLM outside market hours",
                                value=SETTINGS["auto_pause"],
                                on_change=lambda e: (SETTINGS.update(auto_pause=e.value),
                                                     _save_settings()))\
                        .tooltip("Skips the LLM board scan on weekends AND outside 9:30am-"
                                 "3:30pm ET on trading days -- avoids spending API budget "
                                 "analyzing signals nobody can act on for hours. Manual refresh "
                                 "always overrides this.")
                # Row 2: Grid columns + Density
                with ui.row().classes("items-center gap-4 w-full flex-wrap"):
                    ui.label("Columns:").classes("text-sm w-[90px]")
                    def _set_cols2(e) -> None:
                        SETTINGS.update(grid_cols=e.value)
                        _save_settings()
                        grid.refresh(); opportunities.refresh()
                    ui.toggle({1: "1", 2: "2", 3: "3", 4: "4", 5: "5"},
                              value=SETTINGS["grid_cols"], on_change=_set_cols2).props("dense")
                    ui.label("Density:").classes("text-sm")
                    def _set_density(e) -> None:
                        SETTINGS.update(density=e.value)
                        _save_settings()
                        ui.notify(f"Density: {e.value}", type="positive", timeout=1200)
                    ui.toggle({"comfortable": "Comfy", "compact": "Compact"},
                              value=SETTINGS.get("density","comfortable"),
                              on_change=_set_density).props("dense")\
                        .tooltip("Compact reduces card padding & chart heights for dense monitoring")

                from dashboard.core import paper as _paper2

                def _set_overext2(e) -> None:
                    _paper2.OVEREXT_FILTER = bool(e.value)
                    _save_settings()
                    gate_panel.refresh()

                def _set_band2(e) -> None:
                    _paper2.OVEREXT_HI = float(e.value)
                    _paper2.OVEREXT_LO = float(100 - e.value)
                    _save_settings()
                    gate_panel.refresh()
                with ui.row().classes("items-center gap-4 w-full flex-wrap"):
                    ui.checkbox("Block overextended", value=_paper2.OVEREXT_FILTER,
                                on_change=_set_overext2)\
                        .tooltip("skip longs above / shorts below the RSI band (don't chase)")
                    ui.toggle({75: "75/25", 70: "70/30", 65: "65/35"},
                              value=int(_paper2.OVEREXT_HI), on_change=_set_band2).props("dense")

                def _set_tech_paused2(e) -> None:
                    _paper2.TECH_PAUSED = bool(e.value)
                    _save_settings()
                    from dashboard.core import notable_events
                    notable_events.record(
                        "tech investment " +
                        ("PAUSED" if _paper2.TECH_PAUSED else "RESUMED") +
                        " manually (QQQ/XLK/SPY/EEM/ASHR)")
                    gate_panel.refresh(); active_panel.refresh()
                ui.checkbox("Pause tech-concentrated ETFs (core only)", value=_paper2.TECH_PAUSED,
                            on_change=_set_tech_paused2)\
                    .tooltip("Manual override for the CORE strategy only. Blocks new core entries "
                             "and cancels already-pending CORE signals for QQQ/XLK/SPY/EEM/ASHR.")
                with ui.row().classes("items-center gap-4 w-full flex-wrap"):
                    ui.label("Risk/trade:").classes("text-sm w-[90px]")
                    def _set_risk2(e) -> None:
                        setattr(_paper2, "RISK_PER_TRADE", e.value)
                        _save_settings()
                    ui.toggle({0.0025: "0.25%", 0.005: "0.5%", 0.01: "1%", 0.02: "2%"},
                              value=_paper2.RISK_PER_TRADE, on_change=_set_risk2)\
                        .props("dense").tooltip("% of demo equity risked per trade "
                                                "(applied to real equity at order time)")

                ui.separator()
                with ui.row().classes("justify-end w-full"):
                    ui.button("Close", on_click=dlg.close).props("flat")
            dlg.open()

        with ui.row().classes("items-center gap-2 w-full flex-wrap"):
            ui.button("Settings", icon="tune", on_click=_open_settings).props("flat dense")\
                .tooltip("Open settings dialog")
            ui.label("Grid & risk settings moved to dialog to keep Board focused.").classes("text-xs text-grey-6")
            # Density quick toggle (also in dialog)
            ui.toggle({"comfortable": "Comfy", "compact": "Compact"},
                      value=SETTINGS.get("density","comfortable"),
                      on_change=lambda e: (SETTINGS.update(density=e.value),
                                           _save_settings())).props("dense")\
                .tooltip("Quick density switch")

        # B3 2026-08-26: explicit lowercase tab values so the URL hash (#board, #trades, …)
        # is stable even if a display label gets reworded. Selection syncs to the hash and
        # the hash selects the tab on load -- views become bookmarkable/shareable ("open
        # the live dashboard straight on the trades tab"). All JS round-trips are wrapped
        # so any failure degrades to plain non-deep-linked tabs.
        with ui.tabs().classes("w-full") as tabs:
            t_board = ui.tab("board", "Board", icon="dashboard")
            t_alerts = ui.tab("alerts", "Alerts", icon="notifications_active")
            t_signals = ui.tab("signals", "Signals & Gates", icon="traffic")
            t_trades = ui.tab("trades", "Live Trades" if _live else "Paper Trades",
                              icon="receipt_long")
            t_retro = ui.tab("retro", "Retrospective", icon="insights")
        # Alerts-tab spec: stash the tabs element per client so the header bell (a
        # module-level refreshable, outside this page closure) can navigate to it.
        setattr(ui.context.client, "_tabs", tabs)

        def _on_tab_change(e) -> None:
            try:
                v = e.args if isinstance(e.args, str) else (e.args.get("value") if
                                                            isinstance(e.args, dict) else "")
                if v:
                    ui.run_javascript(
                        f"try{{history.replaceState(null,'','#{v}')}}catch(e){{}}", respond=False)
            except Exception:                              # noqa: BLE001 -- cosmetic only
                pass

        def _on_hash_event(e) -> None:
            try:
                h = None
                if isinstance(e.args, dict):
                    h = e.args.get("hash")
                elif isinstance(e.args, list) and e.args and isinstance(e.args[0], dict):
                    h = e.args[0].get("hash")
                v = str(h or "").lstrip("#").lower()
                if v in ("board", "signals", "trades", "retro"):
                    tabs.set_value(v)
            except Exception:                              # noqa: BLE001 -- cosmetic only
                pass

        tabs.on_value_change(_on_tab_change)
        ui.on("qts_hash", _on_hash_event)
        try:
            ui.run_javascript("""
              const emit=()=>{try{emitEvent('qts_hash',{hash:location.hash})}catch(e){}};
              window.addEventListener('hashchange', emit);
              if(location.hash) emit();
            """, respond=False)
        except Exception:                                  # noqa: BLE001
            pass
        with ui.tab_panels(tabs, value=t_board).classes("w-full"):
            with ui.tab_panel(t_board):
                with ui.element("div").classes("grid grid-cols-12 gap-3"):
                    with ui.element("div").classes("col-span-12"):
                        macro_banner()
                    with ui.element("div").classes("col-span-12"):
                        portfolio_panel()
                    with ui.element("div").classes("col-span-12"):
                        active_panel()
            with ui.tab_panel(t_alerts):               # notable events, read at your pace
                alerts_panel()
            with ui.tab_panel(t_signals):
                with ui.column().classes("w-full gap-4"):
                    gate_panel()
                    ui.separator().classes("my-1")
                    opportunities()
                    grid()
            with ui.tab_panel(t_trades):
                paper_panel()
            with ui.tab_panel(t_retro):
                retrospective_panel()

    # live UI tick (1s): clocks + the "x ago" / tick-age labels stay current without touching
    # data (cheap: just re-renders labels from cached state). Fine being per-client -- it's
    # pure display, not real work. The actual data/trading tick runs from a GLOBAL
    # app.on_startup background task (_tick_loop, defined near _tick() above) -- NOT from a
    # per-client timer here anymore (2026-07-12: that was the bug -- see _tick_loop's
    # docstring for why).
    def _ui_tick() -> None:
        clock_row.refresh()
        header_status.refresh()
    _ui_timer = ui.timer(1.0, _ui_tick)
    # FIXED 2026-08-20: found live -- a disconnected client's ui.timer(1.0, ...) doesn't
    # always get cleaned up by NiceGUI itself, so _ui_tick() kept firing every second for a
    # client that no longer exists, hitting Client.check_existence()'s warn_once(...,
    # stack_info=True) EVERY TIME (a full stack-trace capture, not cheap) -- forever, once
    # per stale client. Confirmed in the dashboard's own logs during a real unresponsiveness
    # incident (reproduced safely on paper too): repeated "Client has been deleted but is
    # still being used" warnings alongside the hang. Explicitly cancelling the timer on
    # client delete (NiceGUI's own documented hook for this) stops it at the source, rather
    # than leaving it to accumulate across however many tabs get opened/closed/reloaded over
    # a session's lifetime.
    # FIXED 2026-08-20 (same day): passing the bound method directly
    # (`_ui_timer.cancel`) crashed on_delete's own signature-inspecting dispatch
    # ("Timer.cancel() takes 1 positional argument but 2 were given") -- an explicit
    # no-arg lambda sidesteps that ambiguity entirely.
    ui.context.client.on_delete(lambda: _ui_timer.cancel())
    _refresh_all_panels()   # this client's first paint reflects current STATE immediately,
                            # without waiting for the next 30s background tick


# MT5 link monitor: tracks access-point ping, re-rolls to the fastest on
# sustained degradation. MT5-ONLY -- skip under BROKER=ib (else it polls a broken/
# absent MetaTrader5 every 60s and spams "no attribute initialize").
from dashboard.execution import link_monitor, broker as _bk0  # noqa: E402
if not _bk0.is_ib():
    link_monitor.start()

# Port + title are env-configurable so a LIVE instance can run concurrently with the PAPER
# one (isolated processes): e.g. paper on DASH_PORT=8080 (default), live on 8081.
_DASH_PORT = int(os.environ.get("DASH_PORT", "8080"))
_LIVE = os.environ.get("IB_ALLOW_LIVE", "").lower() in ("1", "true", "yes")
_MODE = "LIVE" if _LIVE else "PAPER"

# ADDED 2026-08-26: /status (JSON or ?fmt=html) + /fleet -- see dashboard/web/fleet.py.
# CORS header on /status is required: the fleet page fetches BOTH instances' /status
# cross-origin (each behind its own hostname), which browsers block without it. The
# snapshot carries only what the dashboard already shows publicly behind the tunnel.
from starlette.responses import JSONResponse, HTMLResponse   # noqa: E402
from nicegui import app as _webapp                           # noqa: E402
from dashboard.web import fleet as _fleet_mod                # noqa: E402

_CORS = {"Access-Control-Allow-Origin": "*"}


async def _status_route(request):
    snap = await run.io_bound(_fleet_mod.status_snapshot)
    if request.query_params.get("fmt") == "html":
        return HTMLResponse(_fleet_mod.render_status_html(snap), headers=_CORS)
    return JSONResponse(snap, headers=_CORS)


async def _fleet_route(request):
    return HTMLResponse(_fleet_mod.render_fleet_html())


_webapp.add_route("/status", _status_route, methods=["GET"])
_webapp.add_route("/fleet", _fleet_route, methods=["GET"])


async def _clear_backoff_route(request):
    from dashboard.web import board_scan
    board_scan._clear_backoff()
    return JSONResponse({"ok": True, "msg": "backoff cleared"}, headers=_CORS)


_webapp.add_route("/api/clear-backoff", _clear_backoff_route, methods=["POST"])

ui.run(title=f"Quantitative Trading System [{_MODE}]", favicon="📈", port=_DASH_PORT, reload=False, show=False)
