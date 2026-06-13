"""NiceGUI dashboard: real-time trade analysis for Gold, Oil and FX.

Decision support only -- it surfaces obvious trends and an LLM read; it never
places a trade.

Run:  python -m dashboard.app      (then open http://localhost:8080)

Refresh model (two tiers, to respect the daily API cap):
  - cheap tier (prices + deterministic scores): runs at the selected interval.
  - LLM board scan: one batched call, throttled to >=10 min and budget-guarded.
"""
from __future__ import annotations

from . import net  # noqa: F401  -- TLS bootstrap first

import datetime as dt
from nicegui import ui, run

from . import service, store
from .instruments import BY_KEY
from .scoring import rank

# ---- settings (live, editable from the UI) --------------------------------
SETTINGS = {"interval_min": 10, "auto_pause": True, "cap": 200}
LLM_MIN_GAP_MIN = 10           # never call the LLM more often than this
_busy = {"flag": False}


# ---- helpers ---------------------------------------------------------------

def _market_open() -> bool:
    """Rough FX market-hours guard for the optional auto-pause.
    FX trades ~24h Mon-Fri. We treat Sat/Sun as closed."""
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
    """Return (label, css) describing the live price source / MT5 connection."""
    live = service.STATE.get("live", {})
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
    off = service.STATE.get("mt5_offset_sec", 0) or 0
    if service.STATE.get("mt5_available") and off:
        broker = now_utc + dt.timedelta(seconds=off)
        parts.append(f"Broker {broker:%H:%M:%S} (UTC{off/3600:+.0f})")
    else:
        parts.append("Broker — (MT5 offset not detected)")
    with ui.row().classes("items-center gap-4"):
        for i, p in enumerate(parts):
            ui.label(p).classes("text-xs font-mono "
                                + ("text-green-8" if i == 2 and off else "text-grey-7"))


@ui.refreshable
def header_status() -> None:
    cap = SETTINGS["cap"]
    used = service.STATE.get("calls_today", 0)  # cached; avoids a DB read each second
    near = used >= cap - 10
    data_txt, data_css = _data_source_text()
    with ui.column().classes("gap-1 w-full"):
        ui.label(data_txt).classes("text-sm " + data_css)
        with ui.row().classes("items-center gap-6 w-full"):
            ui.label("Prices/scores: " + _ago(service.STATE["last_cheap"])).classes("text-sm text-grey-7")
            ui.label("LLM scan: " + _ago(service.STATE["last_llm"])).classes("text-sm text-grey-7")
            ui.label(f"API calls today: {used}/{cap}").classes(
                "text-sm " + ("text-red font-bold" if near else "text-grey-7"))
            ui.label(service.STATE["last_status"]).classes("text-sm text-grey-5 italic")


@ui.refreshable
def macro_banner() -> None:
    note = service.STATE.get("macro_note") or "Run an LLM scan for a macro read."
    with ui.card().classes("w-full bg-blue-1"):
        ui.label("Macro backdrop").classes("text-xs uppercase text-grey-7")
        ui.label(note).classes("text-sm")


def _signal_card(key: str, compact: bool = False):
    score = service.STATE["scores"].get(key)
    sig = service.STATE["llm"].get(key)
    inst = BY_KEY[key]
    # LLM action wins for display if present, else deterministic signal
    action = sig.action if sig else (score.signal if score else "—")
    conf = f"{sig.confidence:.0%}" if sig else ""
    live = service.STATE.get("live", {}).get(key)
    price = live["price"] if live else (score.facts["last_price"] if score else None)
    src = live["src"] if live else service.STATE["sources"].get(key, "")
    with ui.card().classes("min-w-[260px] grow"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(f"{inst.name}").classes("text-base font-bold")
            ui.badge(action, color=SIG_COLOR.get(action, "grey")).classes("text-sm")
        if price is not None:
            with ui.row().classes("items-baseline gap-2"):
                ui.label(f"{price:,.4f}").classes("text-lg")
                tag = "● live" if src == "mt5-tick" else "○ delayed"
                tcolor = "text-green" if src == "mt5-tick" else "text-grey-5"
                ui.label(tag).classes(f"text-xs {tcolor}")
        if score:
            ui.label(score.note).classes("text-xs text-grey-7")
        if sig:
            ui.label(f"LLM: {sig.bias} ({conf}) — {sig.rationale}").classes("text-xs")
            if not compact:
                ui.label(f"Invalid if: {sig.invalidation}").classes("text-xs text-grey-6 italic")
        ui.button("Details", on_click=lambda k=key: _open_detail(k)).props("flat dense").classes("text-xs")


@ui.refreshable
def opportunities() -> None:
    scores = rank(list(service.STATE["scores"].values()))
    obvious = [s for s in scores if s.signal in ("BUY", "SELL")][:4]
    ui.label("Top Opportunities (most obvious trends)").classes("text-lg font-bold")
    if not obvious:
        ui.label("No obviously aligned trends right now — mostly WATCH/WAIT.").classes("text-sm text-grey")
        return
    with ui.row().classes("w-full flex-wrap gap-3"):
        for s in obvious:
            _signal_card(s.key, compact=True)


@ui.refreshable
def grid() -> None:
    ui.label("All instruments").classes("text-lg font-bold")
    with ui.row().classes("w-full flex-wrap gap-3"):
        for s in rank(list(service.STATE["scores"].values())):
            _signal_card(s.key)


def _open_detail(key: str) -> None:
    score = service.STATE["scores"].get(key)
    sig = service.STATE["llm"].get(key)
    with ui.dialog() as dlg, ui.card().classes("min-w-[520px]"):
        ui.label(BY_KEY[key].name).classes("text-xl font-bold")
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
            ui.label(f"Invalidation: {sig.invalidation}").classes("text-sm text-grey-7")
        ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()


@ui.refreshable
def paper_panel() -> None:
    from . import paper
    trades = paper.all_trades()
    closed = [t for t in trades if t["status"] != "OPEN"]
    open_t = [t for t in trades if t["status"] == "OPEN"]

    with ui.row().classes("items-center justify-between w-full"):
        ui.label("Paper Trades — Forward Track Record").classes("text-lg font-bold")
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
            ui.label("No resolved trades yet. They settle as price hits SL/TP or the "
                     "5-day horizon passes.").classes("text-sm text-grey")
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

    # open trades
    if open_t:
        ui.label(f"Open ({len(open_t)})").classes("text-sm font-bold mt-2")
        rows = [{"instrument": t["instrument"], "dir": t["direction"], "method": t["method"],
                 "entry": round(t["entry"], 4), "SL": round(t["sl"], 4),
                 "TP": round(t["tp"], 4), "R:R": t["rr"],
                 "opened": _fmt_ts(t["ts"])} for t in open_t]
        ui.table(rows=rows, columns=[{"name": c, "label": c, "field": c} for c in rows[0]])\
            .classes("w-full").props("dense")
    # recent closed
    if closed:
        ui.label(f"Recent closed ({len(closed)})").classes("text-sm font-bold mt-2")
        rows = [{"instrument": t["instrument"], "dir": t["direction"], "method": t["method"],
                 "status": t["status"], "R": round(t["realized_r"], 2),
                 "opened": _fmt_ts(t["ts"]), "closed": _fmt_ts(t["exit_ts"])}
                for t in closed[:20]]
        ui.table(rows=rows, columns=[{"name": c, "label": c, "field": c} for c in rows[0]])\
            .classes("w-full").props("dense")


@ui.refreshable
def active_panel() -> None:
    """Open positions shown on the Board with live unrealized P&L in R."""
    from . import paper
    open_t = paper.open_trades()
    ui.label(f"Active Trades ({len(open_t)})").classes("text-lg font-bold")
    if not open_t:
        ui.label("No open positions. Setups are logged automatically from "
                 "qualifying signals.").classes("text-sm text-grey")
        return
    with ui.row().classes("w-full flex-wrap gap-3"):
        for t in open_t:
            key = t["instrument"]
            live = service.STATE.get("live", {}).get(key)
            price = live["price"] if live else t["entry"]
            risk = abs(t["entry"] - t["sl"]) or 1e-9
            ur = ((price - t["entry"]) if t["direction"] == "long"
                  else (t["entry"] - price)) / risk
            col = "bg-green-1" if ur >= 0 else "bg-red-1"
            with ui.card().classes(f"min-w-[210px] grow {col}"):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label(BY_KEY[key].name).classes("font-bold")
                    ui.badge(t["direction"],
                             color="positive" if t["direction"] == "long" else "negative")
                ui.label(f"{price:,.4f}").classes("text-base")
                ui.label(f"unrealized: {ur:+.2f} R").classes("text-sm font-bold")
                ui.label(f"entry {t['entry']:.4f} · SL {t['sl']:.4f} · TP {t['tp']:.4f}")\
                    .classes("text-xs text-grey-7")
                ui.label(f"{t['method']} · opened {_fmt_ts(t['ts'])}").classes("text-xs text-grey-6")


@ui.refreshable
def retrospective_panel() -> None:
    """Live equity curve + constraint scorecard for the forward test."""
    from . import paper, journal
    from .retrospective import equity_curve

    trades = paper.all_trades()
    closed = [t for t in trades if t["status"] != "OPEN"]
    rs = [t["realized_r"] for t in closed]
    s = paper.stats(rs)
    curve, max_dd = equity_curve(closed)

    with ui.row().classes("items-center justify-between w-full"):
        ui.label("Retrospective — KPIs & Constraints").classes("text-lg font-bold")
        ui.button("Export full report", icon="download",
                  on_click=_export_retrospective).props("flat dense")
    ui.label("Equity curve is cumulative R over closed trades (entry order). "
             "Constraint scorecard counts how often each gate blocked a candidate "
             "— the evidence for adding/adjusting/removing a rule.")\
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
        _kpi("Expectancy", f"{s['expectancy_R']:+.3f} R", f"n={s['n']}",
             good=(s["expectancy_R"] > 0) if s["n"] else None)
        _kpi("Total / equity", f"{total_r:+.2f} R",
             f"{total_r*paper.RISK_PER_TRADE:+.2%} acct", good=(total_r > 0) if curve else None)
        _kpi("Max drawdown", f"{max_dd:.2f} R",
             f"{-max_dd*paper.RISK_PER_TRADE:.2%} acct", good=(max_dd == 0) if curve else None)
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

    # constraint scorecard
    ui.label("Constraint scorecard").classes("text-sm font-bold mt-2")
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


def _refresh_all_panels() -> None:
    header_status.refresh(); macro_banner.refresh(); opportunities.refresh()
    grid.refresh(); paper_panel.refresh(); active_panel.refresh()
    retrospective_panel.refresh()


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


async def _tick() -> None:
    if _busy["flag"]:
        return
    _busy["flag"] = True
    try:
        now = dt.datetime.now()
        last_cheap = service.STATE["last_cheap"]
        if last_cheap is None or (now - last_cheap).total_seconds() >= SETTINGS["interval_min"] * 60:
            await run.io_bound(service.refresh_news)
            await _do_cheap()
        last_llm = service.STATE["last_llm"]
        gap = max(SETTINGS["interval_min"], LLM_MIN_GAP_MIN) * 60
        if last_llm is None or (now - last_llm).total_seconds() >= gap:
            await _do_llm()
    finally:
        _busy["flag"] = False


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
    from . import paper
    logs = await run.io_bound(paper.place_from_state, service.STATE)
    placed = [l for l in logs if "PLACED" in l]
    paper_panel.refresh(); active_panel.refresh()
    ui.notify(f"Logged {len(placed)} paper trade(s).")


async def _export_results() -> None:
    from . import report
    csvp, repp = await run.io_bound(report.export)
    rep = report.build_report()
    with ui.dialog() as dlg, ui.card().classes("min-w-[680px] max-w-[92vw]"):
        ui.label("Paper-trade report (copy to share)").classes("text-lg font-bold")
        ui.label(f"Saved: {repp}").classes("text-xs text-grey")
        ui.label(f"CSV:   {csvp}").classes("text-xs text-grey")
        ui.code(rep).classes("w-full max-h-[60vh] overflow-auto")
        ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()
    ui.notify("Exported report + CSV to exports/")


async def _export_retrospective() -> None:
    from . import retrospective
    path = await run.io_bound(retrospective.export)
    rep = retrospective.build()
    with ui.dialog() as dlg, ui.card().classes("min-w-[680px] max-w-[92vw]"):
        ui.label("Forward-test retrospective").classes("text-lg font-bold")
        ui.label(f"Saved: {path}").classes("text-xs text-grey")
        ui.code(rep).classes("w-full max-h-[60vh] overflow-auto")
        ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()
    ui.notify("Exported retrospective to exports/")


def _open_archive() -> None:
    from . import paper
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


async def _archive_reset() -> None:
    from . import paper
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


# ---- page ------------------------------------------------------------------

@ui.page("/")
def main_page() -> None:
    service.restore_cache()
    with ui.column().classes("w-full max-w-[1200px] mx-auto gap-3 p-4"):
        ui.label("Trade Analysis — Gold · Oil · FX").classes("text-2xl font-bold")
        ui.label("Decision support, not auto-execution. Verify before risking money.")\
            .classes("text-sm text-grey-6")
        clock_row()

        with ui.row().classes("items-center gap-4 w-full"):
            ui.label("Auto-refresh:").classes("text-sm")
            ui.toggle({1: "1m", 10: "10m", 15: "15m", 30: "30m", 60: "60m"},
                      value=SETTINGS["interval_min"],
                      on_change=lambda e: SETTINGS.update(interval_min=e.value)).props("dense")
            ui.checkbox("Pause LLM on weekends",
                        value=SETTINGS["auto_pause"],
                        on_change=lambda e: SETTINGS.update(auto_pause=e.value))
            ui.button("Manual refresh", icon="refresh", on_click=_manual_refresh).props("color=primary")
            ui.button("Log trades now", icon="playlist_add", on_click=_log_trades_now).props("flat")

        header_status()

        with ui.tabs().classes("w-full") as tabs:
            t_board = ui.tab("Board", icon="dashboard")
            t_trades = ui.tab("Paper Trades", icon="receipt_long")
            t_retro = ui.tab("Retrospective", icon="insights")
        with ui.tab_panels(tabs, value=t_board).classes("w-full"):
            with ui.tab_panel(t_board):
                macro_banner()
                active_panel()
                opportunities()
                grid()
            with ui.tab_panel(t_trades):
                paper_panel()
            with ui.tab_panel(t_retro):
                retrospective_panel()

    # initial load + periodic master tick (30s); the tick decides what actually runs
    # live UI tick (1s): clocks + the "x ago" / tick-age labels stay current
    # without touching data (cheap: just re-renders labels from cached state).
    def _ui_tick() -> None:
        clock_row.refresh()
        header_status.refresh()
    ui.timer(1.0, _ui_tick)
    ui.timer(0.1, _tick, once=True)      # kick off immediately on first load
    ui.timer(30.0, _tick)                # master heartbeat


ui.run(title="Trade Analysis", port=8080, reload=False, show=False)
