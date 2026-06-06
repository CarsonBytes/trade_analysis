"""Shared state and structured outputs for the multi-agent analyst.

Every agent returns a *typed* object (pydantic). This is deliberate: it stops
the LLM from rambling, makes outputs testable, and forces each agent to commit
to specific fields you can audit later against what actually happened.
"""
from __future__ import annotations

from typing import Literal, TypedDict
from pydantic import BaseModel, Field


# ---- per-agent structured outputs -----------------------------------------

class RegimeView(BaseModel):
    regime: Literal["trend_up", "trend_down", "range", "high_vol"] = Field(
        description="The dominant market regime right now."
    )
    confidence: float = Field(ge=0, le=1, description="0-1 confidence in this label.")
    rationale: str = Field(description="2-3 sentences grounded ONLY in the provided facts.")


class TechnicalView(BaseModel):
    direction: Literal["long", "short", "neutral"]
    strength: int = Field(ge=1, le=5, description="1=weak signal, 5=strong alignment across timeframes.")
    key_support: float = Field(description="Nearest meaningful support price from the facts.")
    key_resistance: float = Field(description="Nearest meaningful resistance price from the facts.")
    rationale: str


class SentimentView(BaseModel):
    score: float = Field(ge=-10, le=10, description="-10 very bearish .. +10 very bullish. 0 if no news.")
    key_events: list[str] = Field(default_factory=list, description="Headlines/events that drove the score.")
    rationale: str


class Decision(BaseModel):
    action: Literal["BUY", "SELL", "WAIT"]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(description="How the regime, technical and sentiment views combine.")
    invalidation: str = Field(description="The specific condition/price that would prove this thesis WRONG.")
    disagreements: str = Field(description="Where the agents conflicted, and how you weighed it. '' if none.")


class RiskAssessment(BaseModel):
    final_action: Literal["BUY", "SELL", "WAIT"]
    vetoed: bool
    max_position_units: float
    stop_price: float
    reasons: list[str]


# ---- LangGraph state -------------------------------------------------------

class AnalystState(TypedDict, total=False):
    symbol: str
    facts: dict          # deterministic market facts (computed in code)
    facts_text: str      # human/LLM-readable summary of the facts
    news: list[str]      # recent headlines (may be empty)
    account_equity: float
    risk_per_trade: float

    regime: RegimeView
    technical: TechnicalView
    sentiment: SentimentView
    decision: Decision
    risk: RiskAssessment
