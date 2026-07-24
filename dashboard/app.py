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
from nicegui import app, ui, run

# Mode MUST be resolved before importing anything that touches the DB (service -> paper/store
# compute their DB path at IMPORT time from DASH_DB_NAME). `store` itself is lightweight/self-
# contained (stdlib only), so it's safe to import this early.
from dashboard.core import store


def _resolve_mode() -> str:
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
        os.environ["DASH_DB_NAME"] = "dashboard_live.db"  # SEPARATE journal/history from paper
    else:
        os.environ.pop("IB_ALLOW_LIVE", None)            # paper: guard stays paper-only
        os.environ["DASH_DB_NAME"] = "dashboard.db"       # the original/paper journal
    return mode


DASH_MODE = _resolve_mode()

from dashboard.web import service                          # noqa: E402 -- AFTER mode resolution
from dashboard.instruments import BY_KEY, active_by_key     # noqa: E402
from dashboard.core.scoring import rank                     # noqa: E402

# ---- settings (live, editable from the UI) --------------------------------
# cheap_min: prices/scores/trade-resolution interval (deterministic, free).
# llm_min:   LLM macro/news scan interval (independent; slow-moving, budgeted).
SETTINGS = {"cheap_min": 1, "llm_min": 15, "auto_pause": True,
            "cap": 200, "grid_cols": 4, "chart_period": "All", "chart_scale": "Truncated",
            "chart_view": "P&L (ex-deposits)"}
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
            "risk_per_trade": _p.RISK_PER_TRADE,
            "overext_filter": _p.OVEREXT_FILTER, "overext_hi": _p.OVEREXT_HI})
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
                 "chart_scale", "chart_view"):
            if k in saved:
                SETTINGS[k] = saved[k]
        if "risk_per_trade" in saved:
            _p.RISK_PER_TRADE = float(saved["risk_per_trade"])
        if "overext_filter" in saved:
            _p.OVEREXT_FILTER = bool(saved["overext_filter"])
        if "overext_hi" in saved:
            _p.OVEREXT_HI = float(saved["overext_hi"])
            _p.OVEREXT_LO = float(100 - saved["overext_hi"])
    except Exception:                                  # noqa: BLE001
        pass


_load_settings()                                       # apply persisted settings at import


# ---- helpers ---------------------------------------------------------------

def _market_open() -> bool:
    """Rough market-hours guard for the optional auto-pause. Treats Sat/Sun as closed
    (Mon-Fri open); does NOT check intraday hours, so it can still fire outside the
    9:30-16:00 window on a trading day (the broker itself enforces that at order time).

    FIXED 2026-07-11: this box's system clock is Asia/Hong_Kong (UTC+8), 12h ahead of
    US Eastern (the market this account actually trades, BROKER=ib/UNIVERSE=etf --
    NYSE-listed ETFs). Using the LOCAL weekday meant HK Sat 00:00-04:00 (still Fri
    12:00-16:00 ET, regular trading hours) was wrongly treated as closed, and HK Mon
    00:00-21:30 (still Sun noon - Mon pre-market ET) was wrongly treated as open --
    roughly half a day of misalignment at each week boundary. Confirmed live: the
    auto-pause kicked in at HK Sat 00:00:14, which was Fri 12:00pm ET -- cutting off
    the rest of Friday's real trading session. For the MT5/FX legacy path (~24h
    market, no single relevant exchange timezone) local weekday is kept as-is."""
    from dashboard.instruments import _ib_broker
    if _ib_broker():
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("America/New_York"))
    else:
        now = dt.datetime.now()
    return now.weekday() < 5  # Mon-Fri


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


# ---- refreshable panels ----------------------------------------------------

def _fmt_ts(s: str) -> str:
    """Format a stored timestamp for display in the user's LOCAL timezone.
    Stored values are UTC (the canonical form); we convert to local here."""
    if not s:
        return "—"
    try:
        d = dt.datetime.fromisoformat(str(s))
    except Exception:
        return str(s).replace("T", " ")[:16]
    if d.tzinfo is not None:        # UTC-aware -> local wall time
        d = d.astimezone()
    return d.strftime("%Y-%m-%d %H:%M")


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
    loc = now_utc.astimezone()
    loc_off = loc.utcoffset().total_seconds() / 3600
    parts = [f"Local {loc:%H:%M:%S} (UTC{loc_off:+.0f})", f"UTC {now_utc:%H:%M:%S}"]
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
        with ui.row().classes("items-baseline gap-1"):
            ui.label("broker:").classes("text-xs text-grey-6")
            ui.label(broker_txt).classes(f"text-xs {broker_colour}")
        with ui.row().classes("items-baseline gap-1"):
            ui.label("reconcile:").classes("text-xs text-grey-6")
            rec_label = ui.label(rec_txt).classes(f"text-xs {rec_colour}")
            if rec_tooltip:
                rec_label.tooltip(rec_tooltip)


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
    gtable = ui.table(rows=rows,
             columns=[{"name": c, "label": "" if c == "detail" else c,
                       "field": c,
                       "align": "left" if c in ("blocked by", "status", "instrument") else "center",
                       "sortable": c in ("instrument", "strength", "edge", "status")}
                      for c in cols])\
        .classes("w-full").props("dense")
    gtable.add_slot("body-cell-detail", '''
        <q-td :props="props">
            <q-btn flat dense size="sm" icon="info" color="primary"
                   @click="() => $parent.$emit('detail', props.row.key)" />
        </q-td>
    ''')
    gtable.on("detail", lambda e: _open_detail(e.args))


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


@ui.refreshable
def paper_panel() -> None:
    from dashboard.core import paper
    from dashboard.execution import broker as _bk
    trades = paper.all_trades()
    closed = [t for t in trades if t["status"] != "OPEN"]
    open_t = [t for t in trades if t["status"] == "OPEN"]

    _title = "Live Trades — Track Record" if _bk.is_live() else "Paper Trades — Forward Track Record"
    with ui.row().classes("items-center justify-between w-full"):
        ui.label(_title).classes("text-lg font-bold")
        with ui.row().classes("gap-1"):
            ui.button("Export results", icon="download", on_click=_export_results).props("flat dense")
            ui.button("Archive & reset", icon="inventory_2", on_click=_archive_reset).props("flat dense")
            ui.button("View archive", icon="history", on_click=_open_archive).props("flat dense")
    ui.label("Auto-logged from qualifying signals (both SL/TP methods). "
             "Expectancy in R is the number that matters, not win rate. "
             "Times shown in your local timezone.")\
        .classes("text-xs text-grey-6")

    # stats grouped by method
    methods = sorted({t["method"] for t in closed})
    with ui.row().classes("w-full flex-wrap gap-3"):
        if not closed:
            # FIXED 2026-07-23: this said "5-day horizon" -- wrong on two counts.
            # HORIZON_DAYS=5 is a BAR count (weekly bars), not calendar days; the actual
            # calendar-day figure is HORIZON_CAL=35 (5 weekly bars), already referenced
            # correctly elsewhere in the codebase (e.g. the pending-card ETA text). This one
            # line never got updated and understated how long a trade can sit unresolved by 7x.
            ui.label("No resolved trades yet. They settle as price hits SL/TP or the "
                     f"{paper.HORIZON_CAL}-day horizon passes.").classes("text-sm text-grey")
        for m in methods:
            rs = [t["realized_r"] for t in closed if t["method"] == m]
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

    # open trades (selectable -> archive specific records)
    if open_t:
        with ui.row().classes("items-center gap-2 mt-2"):
            ui.label(f"Open ({len(open_t)})").classes("text-sm font-bold")
            ui.button("Archive selected", icon="archive",
                      on_click=lambda: _archive_records(open_tbl)).props("flat dense")
        rows = [{"id": t["id"], "instrument": t["instrument"], "dir": t["direction"],
                 "method": t["method"], "entry": round(t["entry"], 4),
                 "SL": round(t["sl"], 4), "TP": round(t["tp"], 4), "R:R": t["rr"],
                 "funded": "✓ broker" if t["id"] in _executed else "○ signal only",
                 "opened": _fmt_ts(t["ts"])} for t in open_t]
        open_tbl = ui.table(rows=rows, row_key="id", selection="multiple",
                            columns=[{"name": c, "label": c, "field": c} for c in rows[0]])\
            .classes("w-full").props("dense")
    # recent closed (selectable -> archive specific records)
    if closed:
        with ui.row().classes("items-center gap-2 mt-2"):
            ui.label(f"Recent closed ({len(closed)})").classes("text-sm font-bold")
            ui.button("Archive selected", icon="archive",
                      on_click=lambda: _archive_records(closed_tbl)).props("flat dense")
        rows = [{"id": t["id"], "instrument": t["instrument"], "dir": t["direction"],
                 "method": t["method"], "status": t["status"],
                 "R": round(t["realized_r"], 2),
                 "funded": "✓ broker" if t["id"] in _executed else "○ signal only",
                 "opened": _fmt_ts(t["ts"]),
                 "closed": _fmt_ts(t["exit_ts"])} for t in closed[:20]]
        closed_tbl = ui.table(rows=rows, row_key="id", selection="multiple",
                              columns=[{"name": c, "label": c, "field": c} for c in rows[0]])\
            .classes("w-full").props("dense")\
            .tooltip("'R' is what the signal-logic scored regardless of funding -- "
                     "'○ signal only' rows never had a real broker order, see the "
                     "Retrospective tab for broker-executed-only KPIs")



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

    buckets: dict[str, dict] = {}
    for t in closed:
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
    hist = hist or []
    if not hist:
        return []
    ccy = hist[0][2] if len(hist[0]) > 2 else "USD"
    usd_per_ccy = ib_client._PEG_USD_PER.get(ccy, 1.0)
    adj = paper.deposit_adjusted_series(hist, flows)
    month_end_usd: dict[str, float] = {}
    for (ts, _v, _c), av in zip(hist, adj):
        m = dt.datetime.fromtimestamp(ts).strftime("%Y-%m")
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
    hist = hist or []
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
        _lc = service.STATE.get("last_cheap")
        if _lc is not None:
            ui.label(f"updated {_ago(_lc)}").classes("text-xs text-grey-6")
        elif service.STATE.get("portfolio_ts"):
            _t = dt.datetime.fromtimestamp(service.STATE["portfolio_ts"])
            ui.label(f"last refreshed {_t.strftime('%m-%d %H:%M')} · refreshing…")\
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
            _cash_bits.append(f"USD ${usd_c:,.0f}" + (" ⚠" if _stuck else ""))
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
        ui.label(f"Account value over time ({ccy})").classes("text-sm font-bold")
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
        xs = [dt.datetime.fromtimestamp(h[0]).strftime("%m-%d %H:%M") for h in _whist]
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
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": xs, "boundaryGap": False},
            "yAxis": ({"type": "value", "name": ccy, "min": 0}
                     if (_zero_base and not _use_adj)     # P&L can go negative -- never clip at 0
                     else {"type": "value", "name": ccy, "scale": True}),
            "series": [{"type": "line", "data": ys, "smooth": True, "areaStyle": {},
                        "lineStyle": {"width": 2},
                        "itemStyle": {"color": "#16a34a" if total_pl >= 0 else "#dc2626"},
                        "markLine": ({"silent": True, "symbol": "none", "data": _marks}
                                    if _marks else None)}],
            "grid": {"left": 75, "right": 20, "top": 20, "bottom": 45},
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
                dxs.append(dt.datetime.fromtimestamp(h[0]).strftime("%m-%d %H:%M"))
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
    for pid, p in positions.items():
        mv_usd = p["volume"] * p["open"] + p.get("profit", 0.0)
        raw.append((id_to_sym.get(pid, str(pid)), mv_usd * usd_to_base, mv_usd))
    if sgov_base > 0:
        raw.append((f"SGOV {sgov_yld}", sgov_base, sgov_base * base_to_usd))
    if cash is not None and cash > 0:
        raw.append(("Cash buffer", cash, cash * base_to_usd))
    total_base = sum(b for _, b, _ in raw) or 1.0
    # FULLY precomputed labels (USD actual + ccy converted + %) baked into the slice name
    # -> no ECharts {..} templates rendered; details sit on each slice's title, not the tooltip.
    slices = [{"value": round(b, 2),
               "name": f"{s} {b / total_base * 100:.0f}%\n${u:,.0f} / {ccy} {b:,.0f}"}
              for s, b, u in raw]
    if slices:
        ui.echart({
            "tooltip": {"show": False},
            "legend": {"show": False},
            "series": [{"type": "pie", "radius": ["35%", "60%"],
                        "center": ["50%", "50%"], "data": slices,
                        "label": {"show": True, "position": "outside", "fontSize": 9,
                                  "formatter": "{b}"},
                        "labelLine": {"show": True}}],
        }).classes("w-full h-80").tooltip(
            f"Allocation — each slice labelled with USD (actual) + {ccy} (converted) + %")


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
    from dashboard.core import paper
    from dashboard.data import contracts
    stop_per_share = abs(t["entry"] - t["sl"])
    if eq is None or stop_per_share <= 0:
        return "Broker isn't connected right now — this will be retried automatically once it reconnects.", "retrying"
    needed = contracts.min_equity_for_1_share(stop_per_share, paper.RISK_PER_TRADE)
    if eq < needed:
        return (f"Account isn't big enough yet to buy even 1 share of this at the current "
                f"risk setting (needs ~${needed:,.0f}, you have ~${eq:,.0f}) — this will sit "
                f"here until the account grows, it won't place on its own.", "stuck")
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
        return (f"Needs ~${t['entry']:,.0f}/share, ~${room:,.0f} of room left.{eta}",
               "retrying")
    return "Just logged a moment ago — should reach the broker within the next check (about a minute).", "retrying"


def _trade_card(t: dict, pos: dict | None, reason: str | None = None,
                status: str | None = None) -> None:
    key = t["instrument"]
    live = service.STATE.get("live", {}).get(key)
    price = live["price"] if live else t["entry"]
    # prefer the REAL MT5 fill price when this trade is on the demo, so
    # the card matches what the MT5 terminal shows (not the paper entry).
    entry = pos["open"] if pos else t["entry"]
    risk = abs(entry - t["sl"]) or 1e-9
    ur = ((price - entry) if t["direction"] == "long"
          else (entry - price)) / risk
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
        spark = service.STATE.get("spark", {}).get(key)
        if spark:                                  # same sparkline as Top Opportunities
            up = spark[-1] >= spark[0]
            ui.html(_sparkline_svg(spark, up, h=32)).classes("w-full")
        if pos:                                   # P&L in account base ccy (HKD)
            from dashboard.data import ib_client
            _acct = service.STATE.get("account") or {}
            _ccy = _acct.get("_ccy", "")
            _f = 1.0 / ib_client._PEG_USD_PER.get(_ccy, 1.0)
            pnl = f"  ({_ccy} {pos['profit'] * _f:+,.0f})"
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
        src = f"{_bk.name()} fill" if pos else "logged, unconfirmed"
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
        ui.button("Details", on_click=lambda k=key: _open_detail(k)).props("flat dense").classes("text-xs")


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


@ui.refreshable
def active_panel() -> None:
    """Open positions shown on the Board with live unrealized P&L in R. Splits
    CONFIRMED (a real, broker-mirrored position) from PENDING (a signal that fired
    and was logged, but never actually got sized/placed on the broker -- e.g. an
    account too small to fund it) -- these used to be silently counted together as
    one misleading "Active Trades (N)" total with no distinction."""
    from dashboard.core import paper
    open_t = paper.open_trades()
    positions = service.STATE.get("positions", {})
    confirmed = [t for t in open_t if positions.get(t["id"])]
    pending = [t for t in open_t if not positions.get(t["id"])]
    hdr = f"Active Trades ({len(confirmed)} open"
    hdr += f" · {len(pending)} pending)" if pending else ")"
    ui.label(hdr).classes("text-lg font-bold")
    from dashboard.execution import broker as _bk
    # computed ONCE for the whole render -- both _fundable_count() and _pending_reason()
    # used to each call _bk.equity_usd() independently (a real broker round-trip), once per
    # pending CARD for the latter; see the 2026-07-13 fix note on _pending_reason().
    eq = _bk.equity_usd() if _bk.is_ib() else None
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
        with ui.row().classes("w-full flex-wrap gap-3"):
            for t in confirmed:
                _trade_card(t, positions.get(t["id"]))
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
        room = _bk.portfolio_room_usd() if _bk.is_ib() else None
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
            items = groups[_status]
            if not items:
                continue
            with ui.row().classes("w-full flex-wrap gap-3"):
                for t, msg in items:
                    _trade_card(t, None, reason=msg, status=_status)


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
    else:
        ui.label("No closed trades yet — the equity curve appears as trades settle.")\
            .classes("text-sm text-grey")

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
                              {"name": "trend", "label": "trend $", "field": "trend", "align": "right"},
                              {"name": "sleeve", "label": "sleeve $", "field": "sleeve", "align": "right"},
                              {"name": "other", "label": "other $", "field": "other", "align": "right"},
                              {"name": "total", "label": "total $", "field": "total", "align": "right"}])\
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


def _refresh_all_panels() -> None:
    header_status.refresh(); health_banner.refresh(); macro_banner.refresh()
    opportunities.refresh(); grid.refresh(); paper_panel.refresh(); active_panel.refresh()
    gate_panel.refresh(); retrospective_panel.refresh(); portfolio_panel.refresh()


# ---- refresh orchestration -------------------------------------------------

async def _do_cheap() -> None:
    await run.io_bound(service.refresh_cheap)
    _refresh_all_panels()


async def _do_llm(force: bool = False) -> None:
    # `force` (manual refresh) overrides the weekend auto-pause -- an explicit
    # user click should always be honoured (budget permitting).
    if not force and SETTINGS["auto_pause"] and not _market_open():
        service.STATE["last_status"] = "market closed (auto-pause) — LLM skipped"
        header_status.refresh(); return
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


# ---- page ------------------------------------------------------------------

@ui.page("/")
def main_page() -> None:
    service.restore_cache()
    _live = os.environ.get("IB_ALLOW_LIVE", "").lower() in ("1", "true", "yes")
    with ui.column().classes("w-full max-w-[1200px] mx-auto gap-3 p-4"):
        with ui.row().classes("items-center gap-3 w-full"):
            ui.label("Quantitative Trading System").classes("text-2xl font-bold")
            ui.button(icon="info", on_click=_open_info_modal).props("flat dense round size=sm")\
                .tooltip("Session info: connection clocks, LLM budget, account detail")
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
                    f"equity threshold ~US${_pp.PHASE2_NAV_USD:,.0f} "
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
        # 2026-07-24: the static mode-explainer sentence and clock_row() used to render here
        # on every visit -- the sentence is redundant with the "● LIVE — REAL MONEY" /
        # "● PAPER" badge right above it, and the clocks are rarely the first thing anyone
        # needs. Both moved into the ⓘ info modal next to the title (_open_info_modal()).

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
        with ui.row().classes("items-center gap-2 w-full"):
            ui.button("Manual refresh", icon="refresh", on_click=_manual_refresh).props("color=primary")
            ui.button("Log trades now", icon="playlist_add", on_click=_log_trades_now).props("flat")
            from dashboard.execution import broker as _bk_hdr
            if _bk_hdr.is_ib():
                ui.button("Withdraw", icon="savings", on_click=_open_withdraw).props("flat")\
                    .tooltip("Prepare a cash withdrawal from SGOV/cash shield first (never Core); "
                             "you still transfer the money manually in IBKR")
            ui.button("Restart", icon="restart_alt", on_click=_confirm_restart)\
                .props("flat color=negative")\
                .tooltip("exit the app so the watchdog relaunches it fresh (~10s); "
                         "if the IB Gateway link is down, also force-kills and "
                         "relaunches it (~30-60s + 2FA if prompted)")

        with ui.expansion("Settings", icon="tune").classes("w-full"):
            with ui.row().classes("items-center gap-4 w-full"):
                ui.label("LLM scan:").classes("text-sm")
                ui.toggle({15: "15m", 30: "30m", 60: "60m", 120: "2h", 240: "4h"},
                          value=SETTINGS["llm_min"],
                          on_change=lambda e: (SETTINGS.update(llm_min=e.value),
                                               _save_settings())).props("dense")
                ui.checkbox("Pause LLM on weekends",
                            value=SETTINGS["auto_pause"],
                            on_change=lambda e: (SETTINGS.update(auto_pause=e.value),
                                                 _save_settings()))
                ui.label("Columns:").classes("text-sm")

                def _set_cols(e) -> None:
                    SETTINGS.update(grid_cols=e.value)
                    _save_settings()
                    grid.refresh(); opportunities.refresh()
                ui.toggle({1: "1", 2: "2", 3: "3", 4: "4", 5: "5"},
                          value=SETTINGS["grid_cols"], on_change=_set_cols).props("dense")

                from dashboard.core import paper as _paper

                def _set_overext(e) -> None:
                    _paper.OVEREXT_FILTER = bool(e.value)
                    _save_settings()
                    gate_panel.refresh()

                def _set_band(e) -> None:
                    _paper.OVEREXT_HI = float(e.value)
                    _paper.OVEREXT_LO = float(100 - e.value)
                    _save_settings()
                    gate_panel.refresh()
                ui.checkbox("Block overextended", value=_paper.OVEREXT_FILTER,
                            on_change=_set_overext)\
                    .tooltip("skip longs above / shorts below the RSI band (don't chase)")
                ui.toggle({75: "75/25", 70: "70/30", 65: "65/35"},
                          value=int(_paper.OVEREXT_HI), on_change=_set_band).props("dense")
                ui.label("Risk/trade:").classes("text-sm")
                def _set_risk(e) -> None:
                    setattr(_paper, "RISK_PER_TRADE", e.value)
                    _save_settings()
                ui.toggle({0.0025: "0.25%", 0.005: "0.5%", 0.01: "1%", 0.02: "2%"},
                          value=_paper.RISK_PER_TRADE, on_change=_set_risk)\
                    .props("dense").tooltip("% of demo equity risked per trade "
                                            "(applied to real equity at order time); remembered across restarts")

        with ui.tabs().classes("w-full") as tabs:
            t_board = ui.tab("Board", icon="dashboard")
            t_signals = ui.tab("Signals & Gates", icon="traffic")
            t_trades = ui.tab("Live Trades" if _live else "Paper Trades", icon="receipt_long")
            t_retro = ui.tab("Retrospective", icon="insights")
        with ui.tab_panels(tabs, value=t_board).classes("w-full"):
            with ui.tab_panel(t_board):                # statistics only
                macro_banner()
                portfolio_panel()
                active_panel()
            with ui.tab_panel(t_signals):              # why trades fire + what's ranking
                gate_panel()
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
    ui.timer(1.0, _ui_tick)
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
ui.run(title=f"Quantitative Trading System [{_MODE}]", port=_DASH_PORT, reload=False, show=False)
