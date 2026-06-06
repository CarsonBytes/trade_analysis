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
SETTINGS = {"interval_min": 15, "auto_pause": True, "cap": 200}
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
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    return f"{int(secs // 3600)}h ago"


SIG_COLOR = {"BUY": "positive", "SELL": "negative", "WAIT": "grey", "WATCH": "grey-6"}


# ---- refreshable panels ----------------------------------------------------

@ui.refreshable
def header_status() -> None:
    cap = SETTINGS["cap"]
    used = store.calls_today()
    near = used >= cap - 10
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
    price = score.facts["last_price"] if score else None
    with ui.card().classes("min-w-[260px] grow"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(f"{inst.name}").classes("text-base font-bold")
            ui.badge(action, color=SIG_COLOR.get(action, "grey")).classes("text-sm")
        if price is not None:
            ui.label(f"{price:,.4f}").classes("text-lg")
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


def _refresh_all_panels() -> None:
    header_status.refresh(); macro_banner.refresh(); opportunities.refresh(); grid.refresh()


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


# ---- page ------------------------------------------------------------------

@ui.page("/")
def main_page() -> None:
    service.restore_cache()
    with ui.column().classes("w-full max-w-[1200px] mx-auto gap-3 p-4"):
        ui.label("Trade Analysis — Gold · Oil · FX").classes("text-2xl font-bold")
        ui.label("Decision support, not auto-execution. Verify before risking money.")\
            .classes("text-sm text-grey-6")

        with ui.row().classes("items-center gap-4 w-full"):
            ui.label("Auto-refresh:").classes("text-sm")
            ui.toggle({1: "1m", 10: "10m", 15: "15m", 30: "30m", 60: "60m"},
                      value=SETTINGS["interval_min"],
                      on_change=lambda e: SETTINGS.update(interval_min=e.value)).props("dense")
            ui.checkbox("Pause LLM on weekends",
                        value=SETTINGS["auto_pause"],
                        on_change=lambda e: SETTINGS.update(auto_pause=e.value))
            ui.button("Manual refresh", icon="refresh", on_click=_manual_refresh).props("color=primary")

        header_status()
        macro_banner()
        opportunities()
        grid()

    # initial load + periodic master tick (30s); the tick decides what actually runs
    ui.timer(0.1, _tick, once=True)      # kick off immediately on first load
    ui.timer(30.0, _tick)                # master heartbeat


ui.run(title="Trade Analysis", port=8080, reload=False, show=False)
