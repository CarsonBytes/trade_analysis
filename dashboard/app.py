"""NiceGUI dashboard: real-time trade analysis for Gold, Oil and FX.

Decision support only -- it surfaces obvious trends and an LLM read; it never
places a trade.

Run:  python -m dashboard.app      (then open http://localhost:8080)

Refresh model (two tiers, to respect the daily API cap):
  - cheap tier (prices + deterministic scores): runs at the selected interval.
  - LLM board scan: one batched call, throttled to >=10 min and budget-guarded.
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401  -- TLS bootstrap first

import datetime as dt
from nicegui import ui, run

from dashboard.web import service
from dashboard.core import store
from dashboard.instruments import BY_KEY, active_by_key
from dashboard.core.scoring import rank

# ---- settings (live, editable from the UI) --------------------------------
# cheap_min: prices/scores/trade-resolution interval (deterministic, free).
# llm_min:   LLM macro/news scan interval (independent; slow-moving, budgeted).
SETTINGS = {"cheap_min": 1, "llm_min": 30, "auto_pause": True,
            "cap": 200, "grid_cols": 4}
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
    """Return (label, css) describing the live price source / broker connection."""
    from dashboard.execution import broker
    live = service.STATE.get("live", {})
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
    if _bk.is_ib():
        parts.append("Broker UTC (IBKR)")          # IB timestamps are UTC, no offset
    elif service.STATE.get("mt5_available") and off:
        bkt = now_utc + dt.timedelta(seconds=off)
        parts.append(f"Broker {bkt:%H:%M:%S} (UTC{off/3600:+.0f})")
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
            from dashboard.execution import broker as _broker
            if _broker.is_ib():
                bc = service.STATE.get("broker_conn") or {}
                up = bc.get("available")
                ok = bc.get("ok")
                dot = "●" if up else "○"
                css = ("text-green" if up and ok else "text-orange" if up
                       else "text-red")
                ui.label(f"IBKR Paper: {bc.get('detail', 'gateway down')} {dot}")\
                    .classes(f"text-sm {css}")\
                    .tooltip("BROKER=ib — orders go to the IBKR paper account; "
                             "guard requires a DU… paper account on a paper port")
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
                        .tooltip("IBKR paper account (base ccy): net liquidation, cash, "
                                 "buying power, unrealized P&L")
                ui.label(service.STATE["last_status"]).classes("text-sm text-grey-5 italic")
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
                best_ap, best_ping = lk.get("best_ap"), lk.get("best_ping")
                if best_ap and ap and best_ap != ap and best_ping and ping - best_ping > 15:
                    hint = (f"→ {best_ap} ~{best_ping:.0f}ms"
                            + ("" if lk.get("can_reroll") else " (pin via icon)"))
                    ui.label(hint).classes("text-sm text-orange")\
                        .tooltip("a faster access point is available; the link "
                                 "monitor re-rolls automatically when credentials "
                                 "are set, else pin it via the MT5 connection icon")
            ui.label(service.STATE["last_status"]).classes("text-sm text-grey-5 italic")


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


def _signal_card(key: str, compact: bool = False, width_class: str = "min-w-[260px] grow"):
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
            ui.label(f"{inst.name}").classes("text-base font-bold")
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
    n = SETTINGS.get("grid_cols", 3)
    with ui.element("div").classes("w-full items-stretch").style(
            f"display:grid; grid-template-columns: repeat({n}, minmax(0,1fr)); gap:0.75rem;"):
        for s in obvious:
            _signal_card(s.key, compact=True, width_class="w-full")


@ui.refreshable
def grid() -> None:
    ui.label("Other instruments").classes("text-lg font-bold")
    n = SETTINGS.get("grid_cols", 3)
    top = set(_top_opportunity_keys())  # don't repeat the highlighted ones
    others = [s for s in rank(list(service.STATE["scores"].values())) if s.key not in top]
    if not others:
        ui.label("All current signals are shown in Top Opportunities above.")\
            .classes("text-sm text-grey")
        return
    # inline CSS grid (not Tailwind grid-cols-N, which Tailwind purges when the
    # column count is dynamic) so any chosen column count always renders.
    with ui.element("div").classes("w-full items-stretch").style(
            f"display:grid; grid-template-columns: repeat({n}, minmax(0,1fr)); gap:0.75rem;"):
        for s in others:
            _signal_card(s.key, width_class="w-full")


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
        "instrument": r["key"],
        "action": r["action"],
        "strength": f"{r['strength']}/5",
        "edge": (f"{r['obj_edge']:+.2f}R (n{r['obj_n']})"
                 if r["obj_edge"] is not None else "—"),
        "vol": "ok" if r["vol_ok"] else "low",
        "status": _badge.get(r["status"], r["status"]),
        # an OPEN position's re-entry gates are irrelevant -- don't list them
        "blocked by": ("—" if r["status"] == "OPEN"
                       else "; ".join(r["blocked_by"]) or "—"),
    } for r in rows_data if r["status"] != "WAIT"]
    if not rows:
        ui.label("No directional candidates right now — all instruments are "
                 "WAIT/WATCH.").classes("text-sm text-grey")
        return
    ui.table(rows=rows,
             columns=[{"name": c, "label": c, "field": c,
                       "align": "left" if c in ("blocked by", "status") else "center",
                       "sortable": c in ("instrument", "strength", "edge", "status")}
                      for c in rows[0]])\
        .classes("w-full").props("dense")


def _open_detail(key: str) -> None:
    score = service.STATE["scores"].get(key)
    sig = service.STATE["llm"].get(key)
    with ui.dialog() as dlg, ui.card().classes("min-w-[520px]"):
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
            ui.label(f"Invalidation: {sig.invalidation}").classes("text-sm text-grey-7")
        ui.button("Close", on_click=dlg.close).props("flat")
    dlg.open()


@ui.refreshable
def paper_panel() -> None:
    from dashboard.core import paper
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

    # open trades (selectable -> archive specific records)
    if open_t:
        with ui.row().classes("items-center gap-2 mt-2"):
            ui.label(f"Open ({len(open_t)})").classes("text-sm font-bold")
            ui.button("Archive selected", icon="archive",
                      on_click=lambda: _archive_records(open_tbl)).props("flat dense")
        rows = [{"id": t["id"], "instrument": t["instrument"], "dir": t["direction"],
                 "method": t["method"], "entry": round(t["entry"], 4),
                 "SL": round(t["sl"], 4), "TP": round(t["tp"], 4), "R:R": t["rr"],
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
                 "R": round(t["realized_r"], 2), "opened": _fmt_ts(t["ts"]),
                 "closed": _fmt_ts(t["exit_ts"])} for t in closed[:20]]
        closed_tbl = ui.table(rows=rows, row_key="id", selection="multiple",
                              columns=[{"name": c, "label": c, "field": c} for c in rows[0]])\
            .classes("w-full").props("dense")


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
    ccy = acct.get("_ccy", "")
    nl = acct.get("NetLiquidation")
    cash = acct.get("TotalCashValue")
    gpv = acct.get("GrossPositionValue")
    if nl is None:
        ui.label("Portfolio").classes("text-lg font-bold")
        ui.label("IBKR account data unavailable (gateway down?).").classes("text-sm text-grey")
        return
    positions = service.STATE.get("positions", {})
    usd_to_base = 1.0 / ib_client._PEG_USD_PER.get(ccy, 1.0)   # USD position vals -> base ccy
    upnl = sum(p.get("profit", 0.0) for p in positions.values()) * usd_to_base
    hist, _ts = store.cache_get("equity_history")
    hist = hist or []
    base0 = hist[0][1] if hist else nl                        # value when tracking started
    total_pl = nl - base0
    pct = (total_pl / base0 * 100.0) if base0 else 0.0

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
    invested = (gpv - sgov_base) if gpv is not None else None   # strategy deployment ex-SGOV

    ui.label("Portfolio").classes("text-lg font-bold")
    with ui.row().classes("w-full flex-wrap gap-6 items-stretch"):
        _stat("Total value", _money(nl), "text-grey-9",
              "Net liquidation value of the IBKR paper account")
        _stat("Total P&L", f"{_money(total_pl)}  ({pct:+.2f}%)",
              "text-green" if total_pl >= 0 else "text-red",
              "Account value now minus when tracking began (realized + unrealized)")
        _stat("Unrealized (open)", _money(upnl),
              "text-green" if upnl >= 0 else "text-red",
              "P&L of currently open positions (USD converted at the HKD peg)")
        if invested is not None:
            _stat("Invested", _money(invested), "text-grey-9",
                  "Market value of strategy ETF positions (excludes SGOV cash parking)")
        if cash is not None:
            _stat("Cash (buffer)", _money(cash), "text-grey-9",
                  "Un-parked cash kept available for the strategy")
        if sgov_base > 0:
            _stat("Cash in SGOV", _money(sgov_base), "text-green",
                  "Idle cash parked in SGOV (0-3mo T-bill ETF) earning ~5% — auto-swept")
        fx = service.STATE.get("fx_usd") or {}
        if fx.get("enabled"):
            usd_c = fx.get("usd_cash", 0.0)
            hkd_c = fx.get("hkd_cash", 0.0)
            _stat("USD cash", f"${usd_c:,.0f}",
                  "text-green" if usd_c >= 0 else "text-red",
                  "USD cash balance — negative = margin debit (~5-6% interest); auto-converts "
                  f"idle HKD→USD each cycle. HKD residual: {hkd_c:,.0f}")
        accrued = acct.get("AccruedCash")
        if accrued is not None:
            _stat("Interest accrued", _money(accrued),
                  "text-green" if accrued >= 0 else "text-red",
                  "Cash/SGOV interest accrued this period (IB pays it out monthly). "
                  "Negative = net margin interest owed.")

    # equity line chart (account value over time, base ccy)
    if len(hist) >= 2:
        xs = [dt.datetime.fromtimestamp(h[0]).strftime("%m-%d %H:%M") for h in hist]
        ys = [h[1] for h in hist]
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": xs, "boundaryGap": False},
            "yAxis": {"type": "value", "name": ccy, "scale": True},
            "series": [{"type": "line", "data": ys, "smooth": True, "areaStyle": {},
                        "lineStyle": {"width": 2},
                        "itemStyle": {"color": "#16a34a" if total_pl >= 0 else "#dc2626"}}],
            "grid": {"left": 75, "right": 20, "top": 20, "bottom": 45},
        }).classes("w-full h-56").tooltip("Account value (NetLiq) over time")
    else:
        ui.label("Equity curve builds as snapshots accrue (~one point / 10 min).")\
            .classes("text-sm text-grey mt-1")

    # SGOV cash-parking value over time
    sh, _shts = store.cache_get("sgov_history")
    sh = sh or []
    if sgov_base > 0 or len(sh) >= 2:
        ui.label("Cash parked in SGOV (earning ~5%)").classes("text-sm font-bold mt-2")
    if len(sh) >= 2:
        sxs = [dt.datetime.fromtimestamp(h[0]).strftime("%m-%d %H:%M") for h in sh]
        sys_ = [h[1] for h in sh]
        ui.echart({
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": sxs, "boundaryGap": False},
            "yAxis": {"type": "value", "name": ccy, "scale": True},
            "series": [{"type": "line", "data": sys_, "smooth": True, "areaStyle": {},
                        "lineStyle": {"width": 2}, "itemStyle": {"color": "#0891b2"}}],
            "grid": {"left": 75, "right": 20, "top": 20, "bottom": 45},
        }).classes("w-full h-44").tooltip(f"SGOV holding value ({ccy}) over time")
    elif sgov_base > 0:
        ui.label("SGOV value chart builds as snapshots accrue (~one point / 10 min).")\
            .classes("text-sm text-grey mt-1")

    # allocation pie: strategy positions + SGOV + buffer cash, dual-currency on hover
    id_to_sym = {t["id"]: t["instrument"] for t in paper.open_trades()}
    base_to_usd = 1.0 / usd_to_base if usd_to_base else 0.0   # base ccy -> USD
    slices = []

    def _slice(name_short, base_val, usd_val):
        # name carries BOTH currencies so the tooltip shows actual (USD) + converted (ccy)
        slices.append({"value": round(base_val, 2),
                       "name": f"{name_short}  ·  ${usd_val:,.0f} USD"})
    for pid, p in positions.items():
        mv_usd = p["volume"] * p["open"] + p.get("profit", 0.0)
        _slice(id_to_sym.get(pid, str(pid)), mv_usd * usd_to_base, mv_usd)
    if sgov_base > 0:
        _slice("SGOV ~5%", sgov_base, sgov_base * base_to_usd)
    if cash is not None and cash > 0:
        _slice("Cash buffer", cash, cash * base_to_usd)
    if slices:
        ui.echart({
            "tooltip": {"trigger": "item", "formatter": "{b}<br/>" + ccy + " {c}  ({d}%)"},
            "legend": {"type": "scroll", "bottom": 0, "textStyle": {"fontSize": 9}},
            "series": [{"type": "pie", "radius": ["40%", "65%"],
                        "center": ["50%", "45%"], "data": slices,
                        "label": {"formatter": "{d}%", "fontSize": 10}}],
        }).classes("w-full h-72").tooltip(
            f"Allocation — each slice shows USD (actual) + {ccy} (converted)")


@ui.refreshable
def active_panel() -> None:
    """Open positions shown on the Board with live unrealized P&L in R."""
    from dashboard.core import paper
    open_t = paper.open_trades()
    ui.label(f"Active Trades ({len(open_t)})").classes("text-lg font-bold")
    if not open_t:
        ui.label("No open positions. Setups are logged automatically from "
                 "qualifying signals.").classes("text-sm text-grey")
        return
    positions = service.STATE.get("positions", {})
    with ui.row().classes("w-full flex-wrap gap-3"):
        for t in open_t:
            key = t["instrument"]
            live = service.STATE.get("live", {}).get(key)
            price = live["price"] if live else t["entry"]
            # prefer the REAL MT5 fill price when this trade is on the demo, so
            # the card matches what the MT5 terminal shows (not the paper entry).
            pos = positions.get(t["id"])
            entry = pos["open"] if pos else t["entry"]
            risk = abs(entry - t["sl"]) or 1e-9
            ur = ((price - entry) if t["direction"] == "long"
                  else (entry - price)) / risk
            col = "bg-green-1" if ur >= 0 else "bg-red-1"
            with ui.card().classes(f"min-w-[210px] grow {col}"):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label(active_by_key(key).name).classes("font-bold")
                    ui.badge(t["direction"],
                             color="positive" if t["direction"] == "long" else "negative")
                ui.label(f"{price:,.4f}").classes("text-base")
                if pos:                                   # P&L in account base ccy (HKD)
                    from dashboard.data import ib_client
                    _acct = service.STATE.get("account") or {}
                    _ccy = _acct.get("_ccy", "")
                    _f = 1.0 / ib_client._PEG_USD_PER.get(_ccy, 1.0)
                    pnl = f"  ({_ccy} {pos['profit'] * _f:+,.0f})"
                else:
                    pnl = ""
                ui.label(f"unrealized: {ur:+.2f} R{pnl}").classes("text-sm font-bold")
                from dashboard.execution import broker as _bk
                src = f"{_bk.name()} fill" if pos else "paper"
                ui.label(f"entry {entry:.4f} ({src}) · SL {t['sl']:.4f} · TP {t['tp']:.4f}")\
                    .classes("text-xs text-grey-7")
                tag = f" · #{t['id']}" + (f" ticket {pos['ticket']}" if pos
                                          else f" (not on {_bk.name()})")
                ui.label(f"{t['method']} · opened {_fmt_ts(t['ts'])}{tag}")\
                    .classes("text-xs text-grey-6")


@ui.refreshable
def retrospective_panel() -> None:
    """Live equity curve + constraint scorecard for the forward test."""
    from dashboard.core import paper
    from dashboard.core import journal
    from dashboard.web.retrospective import equity_curve, _demo_executed_ids

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


# connection_panel removed -- access points are switched manually; the header
# already shows the live access point + ping. (See git history / link_monitor
# for the ap-comparison table if you want it back.)


def _refresh_all_panels() -> None:
    header_status.refresh(); macro_banner.refresh(); opportunities.refresh()
    grid.refresh(); paper_panel.refresh(); active_panel.refresh()
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


async def _tick() -> None:
    if _busy["flag"]:
        return
    _busy["flag"] = True
    try:
        now = dt.datetime.now()
        last_cheap = service.STATE["last_cheap"]
        if last_cheap is None or (now - last_cheap).total_seconds() >= SETTINGS["cheap_min"] * 60:
            await run.io_bound(service.refresh_news)
            await _do_cheap()
        last_llm = service.STATE["last_llm"]
        if last_llm is None or (now - last_llm).total_seconds() >= SETTINGS["llm_min"] * 60:
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
    from dashboard.core import paper
    logs = await run.io_bound(paper.place_from_state, service.STATE)
    placed = [l for l in logs if "PLACED" in l]
    paper_panel.refresh(); active_panel.refresh()
    ui.notify(f"Logged {len(placed)} paper trade(s).")


def _restart_server() -> None:
    """Exit the process so the watchdog (DashboardApp task / dashboard.ps1)
    relaunches it fresh with the latest code, ~10s later."""
    import os
    import threading
    from dashboard.core.log import log
    ui.notify("Restarting — the watchdog relaunches in ~10s. Reload the page shortly.",
              type="warning", timeout=9000)
    log.info("restart requested from UI; exiting for watchdog relaunch")
    threading.Timer(1.2, lambda: os._exit(0)).start()


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
    from dashboard.web import retrospective
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


# ---- page ------------------------------------------------------------------

@ui.page("/")
def main_page() -> None:
    service.restore_cache()
    with ui.column().classes("w-full max-w-[1200px] mx-auto gap-3 p-4"):
        ui.label("Trade Analysis — all popular signals").classes("text-2xl font-bold")
        ui.label("Decision support, not auto-execution. Verify before risking money.")\
            .classes("text-sm text-grey-6")
        clock_row()

        with ui.row().classes("items-center gap-4 w-full"):
            ui.label("LLM scan:").classes("text-sm")
            ui.toggle({15: "15m", 30: "30m", 60: "60m", 120: "2h", 240: "4h"},
                      value=SETTINGS["llm_min"],
                      on_change=lambda e: SETTINGS.update(llm_min=e.value)).props("dense")
            ui.checkbox("Pause LLM on weekends",
                        value=SETTINGS["auto_pause"],
                        on_change=lambda e: SETTINGS.update(auto_pause=e.value))
            ui.label("Columns:").classes("text-sm")

            def _set_cols(e) -> None:
                SETTINGS.update(grid_cols=e.value)
                grid.refresh(); opportunities.refresh()
            ui.toggle({1: "1", 2: "2", 3: "3", 4: "4", 5: "5"},
                      value=SETTINGS["grid_cols"], on_change=_set_cols).props("dense")

            from dashboard.core import paper as _paper

            def _set_overext(e) -> None:
                _paper.OVEREXT_FILTER = bool(e.value)
                gate_panel.refresh()

            def _set_band(e) -> None:
                _paper.OVEREXT_HI = float(e.value)
                _paper.OVEREXT_LO = float(100 - e.value)
                gate_panel.refresh()
            ui.checkbox("Block overextended", value=_paper.OVEREXT_FILTER,
                        on_change=_set_overext)\
                .tooltip("skip longs above / shorts below the RSI band (don't chase)")
            ui.toggle({75: "75/25", 70: "70/30", 65: "65/35"},
                      value=int(_paper.OVEREXT_HI), on_change=_set_band).props("dense")
            ui.label("Risk/trade:").classes("text-sm")
            ui.toggle({0.0025: "0.25%", 0.005: "0.5%", 0.01: "1%", 0.02: "2%"},
                      value=_paper.RISK_PER_TRADE,
                      on_change=lambda e: setattr(_paper, "RISK_PER_TRADE", e.value))\
                .props("dense").tooltip("% of demo equity risked per trade "
                                        "(applied to real equity at order time)")
            ui.button("Manual refresh", icon="refresh", on_click=_manual_refresh).props("color=primary")
            ui.button("Log trades now", icon="playlist_add", on_click=_log_trades_now).props("flat")
            ui.button("Restart", icon="restart_alt", on_click=_restart_server)\
                .props("flat color=negative")\
                .tooltip("exit the app so the watchdog relaunches it fresh (~10s)")

        header_status()

        with ui.tabs().classes("w-full") as tabs:
            t_board = ui.tab("Board", icon="dashboard")
            t_trades = ui.tab("Paper Trades", icon="receipt_long")
            t_retro = ui.tab("Retrospective", icon="insights")
        with ui.tab_panels(tabs, value=t_board).classes("w-full"):
            with ui.tab_panel(t_board):
                macro_banner()
                portfolio_panel()
                active_panel()
                gate_panel()
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


# MT5 link monitor: tracks access-point ping, re-rolls to the fastest on
# sustained degradation. MT5-ONLY -- skip under BROKER=ib (else it polls a broken/
# absent MetaTrader5 every 60s and spams "no attribute initialize").
from dashboard.execution import link_monitor, broker as _bk0  # noqa: E402
if not _bk0.is_ib():
    link_monitor.start()

ui.run(title="Trade Analysis", port=8080, reload=False, show=False)
