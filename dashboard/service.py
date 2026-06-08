"""Service layer: holds the live in-memory state and the refresh functions.

Two refresh tiers (this is the whole budget strategy):
  - refresh_cheap(): prices + deterministic scores for ALL instruments. No LLM,
    so it can run as often as the UI asks (every minute is fine).
  - refresh_llm():   one batched board-scan call. Throttled + budget-guarded.
"""
from __future__ import annotations

from . import net  # noqa: F401

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from analyst.features import compute_facts  # quant/
from .instruments import UNIVERSE
from .providers import get_history
from .scoring import score_from_facts, rank, Score
from .news_sources import fetch_headlines
from .board_scan import run_board_scan, InstrumentSignal
from .providers import get_ohlc
from . import store
from . import paper
from . import mt5_client
from .log import log

# ---- live state (single process, so plain dict is fine) --------------------
STATE: dict = {
    "scores": {},          # key -> Score
    "live": {},            # key -> {price, src, spread}  near-tick when MT5 present
    "llm": {},             # key -> InstrumentSignal
    "macro_note": "",
    "news": [],            # list[str]
    "sources": {},         # key -> provider label
    "last_cheap": None,    # datetime
    "last_llm": None,      # datetime
    "last_status": "not run yet",
    "mt5_available": False,
    "calls_today": 0,
    "cap": 200,
}


def _now() -> dt.datetime:
    return dt.datetime.now()


def _calibrate_mt5_offset() -> float:
    """MT5 stamps ticks in the broker's SERVER timezone, so raw age = real age +
    server offset. We estimate the offset as the smallest raw age ever seen from
    a fresh tick (a truly fresh tick has real age ~0, so raw age ~= offset),
    rounded to 30 min, persisted. Subtracting it makes a live tick read ~0s.
    (Converges down automatically; a rare DST forward shift self-heals over time.)"""
    raw = [v.get("age") for v in STATE["live"].values()
           if v.get("src") == "mt5-tick" and v.get("age") is not None]
    fresh = [a for a in raw if 0 <= a < 21600]  # ignore weekend-stale (>6h)
    prev, _ = store.cache_get("mt5_offset_sec")
    prev = prev if isinstance(prev, (int, float)) else None
    if fresh:
        cand = min(fresh)
        off = cand if prev is None else min(prev, cand)
        off = round(off / 1800) * 1800
        store.cache_set("mt5_offset_sec", off)
    else:
        off = prev or 0.0
    STATE["mt5_offset_sec"] = off
    # apply correction: store true freshness in 'age', keep raw for the log
    for v in STATE["live"].values():
        if v.get("src") == "mt5-tick" and v.get("age") is not None:
            v["raw_age"] = v["age"]
            v["age"] = max(0.0, v["age"] - off)
    return off


def _score_one(inst):
    series, source = get_history(inst)
    if series is None:
        return inst.key, None, source, None, None, None
    facts, text = compute_facts(series, inst.key)
    score = score_from_facts(inst.key, facts, text)
    # near-tick live price from MT5 if available, else last bar close (no extra fetch)
    tick = mt5_client.get_tick(inst.mt5)
    if tick is not None:
        live_px, live_src, spread, age = tick["mid"], "mt5-tick", tick["spread"], tick["age_sec"]
    else:
        live_px, live_src, spread, age = float(series.iloc[-1]), source, None, None
    return inst.key, score, source, live_px, live_src, spread, age


def refresh_cheap() -> None:
    """Fetch prices + compute deterministic scores for every instrument."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        for key, score, source, live_px, live_src, spread, age in ex.map(_score_one, UNIVERSE):
            STATE["sources"][key] = source
            if score is not None:
                STATE["scores"][key] = score
            if live_px is not None:
                STATE["live"][key] = {"price": live_px, "src": live_src,
                                      "spread": spread, "age": age}
    STATE["mt5_available"] = mt5_client.is_available()
    STATE["calls_today"] = store.calls_today()
    _calibrate_mt5_offset()
    STATE["last_cheap"] = _now()
    live = STATE["live"]
    n_mt5 = sum(1 for v in live.values() if v.get("src") == "mt5-tick")
    log.info("cheap refresh: %d scored, data source = %s (%d/%d MT5-tick)",
             len(STATE["scores"]),
             "MT5" if n_mt5 else ("yfinance" if live else "none"),
             n_mt5, len(live))
    # resolve any open paper trades against the fresh price action
    try:
        n = paper.resolve_open(get_ohlc)
        STATE["paper_resolved"] = n
        if n:
            log.info("resolved %d paper trade(s) this refresh", n)
    except Exception as e:
        STATE["paper_resolved"] = f"resolve error: {e}"
        log.exception("paper resolution error: %s", e)


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
    STATE["last_status"] = status
    STATE["calls_today"] = store.calls_today()
    log.info("LLM board scan: %s (calls today %d/%d)", status, STATE["calls_today"], cap)
    return status


def restore_cache() -> None:
    """Load the last board scan from disk on startup (no LLM call)."""
    data, ts = store.cache_get("last_board_scan")
    if data:
        STATE["macro_note"] = data.get("macro_note", "")
        STATE["llm"] = {s["key"]: InstrumentSignal(**s) for s in data.get("signals", [])}
        STATE["last_status"] = f"restored cached scan from {ts}"
