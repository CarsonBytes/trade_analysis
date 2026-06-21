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
    invalidation: str = Field(description="specific price/condition that proves this wrong.")


class BoardScan(BaseModel):
    macro_note: str = Field(description="2-3 sentences on the overall macro/risk backdrop.")
    signals: list[InstrumentSignal]


SYSTEM = (
    "You are the head analyst on a trading desk. You are given pre-computed, "
    "factual indicators for several instruments (metals, energy, FX, indices, "
    "crypto) plus recent "
    "headlines. Do NOT invent numbers; reason only from the facts provided. "
    "For each instrument give a bias, an action (BUY/SELL/WAIT), a calibrated "
    "confidence, a one-line rationale, and the explicit invalidation level. "
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


# Only the TOP of the deterministic ranking is worth an LLM deep-dive -- the
# rest are clear WAIT/WATCH. Capping this also keeps the prompt within provider
# input-token limits (free tiers cap at ~4k). Tune to your provider's budget.
MAX_INSTRUMENTS = 10
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
