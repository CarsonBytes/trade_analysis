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
    "cap": 200,
}


def _now() -> dt.datetime:
    return dt.datetime.now()


def _score_one(inst):
    series, source = get_history(inst)
    if series is None:
        return inst.key, None, source, None, None, None
    facts, text = compute_facts(series, inst.key)
    score = score_from_facts(inst.key, facts, text)
    # near-tick live price from MT5 if available, else last bar close (no extra fetch)
    tick = mt5_client.get_tick(inst.mt5)
    if tick is not None:
        live_px, live_src, spread = tick["mid"], "mt5-tick", tick["spread"]
    else:
        live_px, live_src, spread = float(series.iloc[-1]), source, None
    return inst.key, score, source, live_px, live_src, spread


def refresh_cheap() -> None:
    """Fetch prices + compute deterministic scores for every instrument."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        for key, score, source, live_px, live_src, spread in ex.map(_score_one, UNIVERSE):
            STATE["sources"][key] = source
            if score is not None:
                STATE["scores"][key] = score
            if live_px is not None:
                STATE["live"][key] = {"price": live_px, "src": live_src, "spread": spread}
    STATE["last_cheap"] = _now()
    # resolve any open paper trades against the fresh price action
    try:
        STATE["paper_resolved"] = paper.resolve_open(get_ohlc)
    except Exception as e:
        STATE["paper_resolved"] = f"resolve error: {e}"


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
    return status


def restore_cache() -> None:
    """Load the last board scan from disk on startup (no LLM call)."""
    data, ts = store.cache_get("last_board_scan")
    if data:
        STATE["macro_note"] = data.get("macro_note", "")
        STATE["llm"] = {s["key"]: InstrumentSignal(**s) for s in data.get("signals", [])}
        STATE["last_status"] = f"restored cached scan from {ts}"
