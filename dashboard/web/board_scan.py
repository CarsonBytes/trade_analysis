"""Batched LLM board scan: ONE call analyses every instrument at once.

This is the budget-critical design. Instead of 4 calls x N instruments, the
whole board costs a single structured-output call. The deterministic scorer has
already done the ranking for free; the LLM adds judgement, news synthesis and an
explicit invalidation level per instrument.

Respects the daily budget guard: if we're near the cap, it returns None and the
UI keeps showing deterministic data only.
"""
from __future__ import annotations

from typing import Literal

from dashboard.core import net  # noqa: F401
from pydantic import BaseModel, Field

from analyst.llm import make_llm  # from quant/analyst
from dashboard.core import store
from dashboard.core.scoring import Score


class InstrumentSignal(BaseModel):
    key: str = Field(description="instrument key, exactly as given")
    bias: Literal["bullish", "bearish", "neutral"]
    action: Literal["BUY", "SELL", "WAIT"]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(description="1-2 sentences grounded in the provided facts/news.")
    macro_linkage: str = Field(description=
        "Does any theme from YOUR OWN macro_note actually apply to THIS instrument "
        "specifically (e.g. a USD-strength headwind on metals, a shared commodity-complex "
        "driver, risk-off FX flows)? One short sentence, and be concrete about the "
        "MECHANISM (not just 'macro is risk-on') -- e.g. copper isn't necessarily bearish "
        "just because oil spiked on a supply shock, but IS exposed if that same shock is "
        "driving safe-haven USD strength. Say 'none material' if nothing genuinely "
        "connects -- don't force a link that isn't really there.")
    invalidation: str = Field(description="specific price/condition that proves this wrong.")


class BoardScan(BaseModel):
    macro_note: str = Field(description="2-3 sentences on the overall macro/risk backdrop.")
    signals: list[InstrumentSignal]


# ADDED 2026-07-14: macro_linkage field + this paragraph, after a real trade (CPER, placed
# 2026-07-13) got a purely technical rationale ("uptrend, momentum favors continuation")
# despite the SAME board scan's own macro_note flagging Iran/Middle-East tension driving
# safe-haven USD strength -- a real, statistically-supported headwind for copper (-0.54
# correlation with DXY over the trailing 2mo, confirmed against real data) that never made it
# into the per-instrument reasoning. The LLM was identifying macro themes at the board level
# but not systematically checking whether they applied to each instrument it scored --
# forcing a dedicated field (rather than hoping the free-text rationale mentions it) makes
# this reliable and auditable instead of hopeful.
SYSTEM = (
    "You are the head analyst on a trading desk. You are given pre-computed, "
    "factual indicators for several instruments (metals, energy, FX, indices, "
    "crypto) plus recent "
    "headlines. Do NOT invent numbers; reason only from the facts provided. "
    "First form the macro_note (2-3 sentences on the overall backdrop). THEN, for each "
    "instrument, give a bias, an action (BUY/SELL/WAIT), a calibrated confidence, a "
    "one-line rationale, an explicit macro_linkage (does any theme from your OWN "
    "macro_note actually apply to THIS instrument, through what mechanism -- or "
    "genuinely nothing? Say so either way, don't skip this step even when the answer is "
    "'none material'), and the explicit invalidation level. "
    "WAIT is correct when signals conflict or a trend is overextended. Only "
    "count headlines actually relevant to an instrument. You advise a human who "
    "makes the final call -- never overstate confidence."
)


def _facts_block(scores: list[Score]) -> str:
    blocks = []
    for s in scores:
        blocks.append(
            f"### {s.key}  (deterministic: {s.signal}, dir {s.direction}, "
            f"strength {s.strength}/5)\n{s.facts_text}"
        )
    return "\n\n".join(blocks)


# FIXED 2026-07-13: this cap's own assumption ("the rest are clear WAIT/WATCH") is false --
# checked directly against a real day's data: EFA/HYD/HYG/SHY all had a real deterministic
# BUY/SELL that day (rejected on a DIFFERENT gate, trend-strength/RSI) but weren't in the
# top-10 sent here, so they got evaluated with NO llm_sig at all (see evaluate_signal() in
# core/paper.py -- action falls back to the deterministic signal, with none of the LLM's
# news-awareness or "signals conflict/overextended" judgment applied). The original "~4k free
# tier" token concern doesn't apply to this deployment's actual configured model
# (OPENAI_MODEL=gpt-5-mini, a large context window) -- 22 instruments' worth of facts_text
# plus headlines is a small fraction of it. Raised to cover the full active ETF universe (22
# today) with headroom for growth, so every watched instrument gets a real LLM look every
# scan, not just the most "obvious" 10. Cost is still bounded by store.can_call()'s daily
# call-COUNT budget (unaffected by per-call size) -- this doesn't add calls, just completeness
# within the one call already being made.
MAX_INSTRUMENTS = 40
MAX_NEWS = 10


def run_board_scan(scores: list[Score], headlines: list[str],
                   cap: int = 200) -> tuple[BoardScan | None, str]:
    """Returns (BoardScan|None, status). status explains why None if applicable.
    Only the top MAX_INSTRUMENTS of the (already-ranked) scores are sent to the
    LLM -- it deep-dives the most actionable, not the whole board."""
    if not store.can_call(cap=cap):
        return None, f"budget guard: {store.calls_today()}/{cap} calls used today"

    top = scores[:MAX_INSTRUMENTS]
    news = headlines[:MAX_NEWS]
    news_block = "\n".join(f"- {h}" for h in news) or "(no headlines available)"
    human = (
        f"INSTRUMENT FACTS (top {len(top)} by signal strength):\n{_facts_block(top)}\n\n"
        f"RECENT HEADLINES (may be irrelevant; filter yourself):\n{news_block}\n\n"
        "Return a signal for EVERY instrument above, plus a macro_note."
    )
    llm = make_llm().with_structured_output(BoardScan)
    result = llm.invoke([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": human},
    ])
    store.record_call(1)
    return result, "ok"
