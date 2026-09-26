"""The graph nodes.

Two kinds:
  - Deterministic nodes (gather_facts, risk_gate): plain Python, no LLM. The
    risk gate has the FINAL say and can veto the LLM. This is intentional:
    money management must never depend on a model's mood.
  - LLM analyst nodes (regime, technical, sentiment, decision): each gets the
    deterministic facts and returns a typed opinion.
"""
from __future__ import annotations

import os
import time

from langchain_core.messages import SystemMessage, HumanMessage

from .state import (
    AnalystState, AllAnalysts, RegimeView, TechnicalView, SentimentView, Decision, RiskAssessment,
)
from .llm import invoke_with_key_fallback, last_model_used, last_provider_used
from .usage_log import log_usage


def _ask(structured_model, system: str, human: str, kind: str = "analyst"):
    start = time.perf_counter()
    result = invoke_with_key_fallback(
        lambda llm: llm.with_structured_output(structured_model, include_raw=True),
        [SystemMessage(content=system), HumanMessage(content=human)],
    )
    try:
        usage = getattr(result["raw"], "usage_metadata", None) or {}
        log_usage(
            kind=kind,
            model=last_model_used() or os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            latency_ms=int((time.perf_counter() - start) * 1000),
            provider=last_provider_used(),
        )
    except Exception:
        pass  # telemetry only -- never let this affect analysis or trading
    return result["parsed"]


# --- LLM analyst nodes ------------------------------------------------------

def analysts_node(state: AnalystState) -> dict:
    """Collapsed analyst node: regime + technical + sentiment in ONE LLM call.
    ADDED 2026-09-26: saves ~2K input tokens/instrument (facts_text sent once
    instead of 3 times) and reduces latency ~66%. The three roles are
    independent (no cross-dependencies), so batching them in one structured
    output is lossless — the model sees the same facts once and produces all
    three views."""
    news = state.get("news") or []
    headlines_block = ""
    if news:
        headlines = "\n".join(f"- {h}" for h in news)
        headlines_block = (
            f"\n\nHeadlines for {state['symbol']} (for sentiment scoring; "
            f"ignore if not relevant to this instrument):\n{headlines}"
        )
    result = _ask(
        AllAnalysts,
        "You are a trading desk with three analyst roles. Using ONLY the facts "
        "given, produce all three views below. Do not invent numbers. Be "
        "decisive but calibrate confidence.\n\n"
        "1. REGIME: Classify market regime (trend_up/trend_down/range/high_vol), "
        "confidence 0-1, 2-3 sentence rationale grounded in facts.\n"
        "2. TECHNICAL: Direction (long/short/neutral), strength 1-5, key support "
        "and resistance FROM THE FACTS (do not fabricate levels), rationale.\n"
        "3. SENTIMENT: Score -10 (very bearish) to +10 (very bullish) based on "
        "headlines. Only count headlines actually relevant to this instrument; "
        "score 0 if nothing relevant. List key events that drove the score.",
        f"Market facts for {state['symbol']}:\n{state['facts_text']}{headlines_block}",
        kind="analysts",
    )
    return {
        "regime": result.regime,
        "technical": result.technical,
        "sentiment": result.sentiment,
    }


def regime_node(state: AnalystState) -> dict:
    view = _ask(
        RegimeView,
        "You are a market-regime analyst. Classify the current regime using ONLY "
        "the facts given. Do not invent numbers. Be decisive but calibrate confidence.",
        f"Market facts:\n{state['facts_text']}\n\nClassify the regime.",
        kind="regime",
    )
    return {"regime": view}


def technical_node(state: AnalystState) -> dict:
    view = _ask(
        TechnicalView,
        "You are a technical analyst. Give a directional read from multi-timeframe "
        "trend, RSI, and the support/resistance levels in the facts. Pick key levels "
        "from the provided support/resistance. Do not fabricate price levels.",
        f"Market facts:\n{state['facts_text']}\n\nGive your technical view.",
        kind="technical",
    )
    return {"technical": view}


def sentiment_node(state: AnalystState) -> dict:
    news = state.get("news") or []
    if not news:
        return {"sentiment": SentimentView(
            score=0.0, key_events=[],
            rationale="No headlines available; sentiment treated as neutral.",
        )}
    headlines = "\n".join(f"- {h}" for h in news)
    view = _ask(
        SentimentView,
        f"You are a news-sentiment analyst for {state['symbol']}. Score how the "
        "headlines bias this instrument from -10 (very bearish) to +10 (very bullish). "
        "Only count headlines actually relevant to this instrument; ignore noise. "
        "If nothing is relevant, score 0.",
        f"Instrument: {state['symbol']}\nHeadlines:\n{headlines}\n\nScore the sentiment.",
        kind="sentiment",
    )
    return {"sentiment": view}


def decision_node(state: AnalystState) -> dict:
    regime, tech, sent = state["regime"], state["technical"], state["sentiment"]
    # ADDED 2026-09-26: facts_text removed from decision prompt — it was already
    # sent to the analysts node, so re-sending it here wastes ~500 input tokens
    # per instrument with no new information (the decision is based on the
    # analysts' interpretations, not raw facts).
    human = (
        f"Instrument: {state['symbol']}\n\n"
        f"REGIME AGENT: {regime.regime} (conf {regime.confidence:.2f}) - {regime.rationale}\n"
        f"TECHNICAL AGENT: {tech.direction} strength {tech.strength}/5, "
        f"support {tech.key_support:.5f} resistance {tech.key_resistance:.5f} - {tech.rationale}\n"
        f"SENTIMENT AGENT: score {sent.score:+.1f} - {sent.rationale}\n\n"
        "Synthesize a single recommendation. WAIT is a valid and often correct answer. "
        "State the invalidation level explicitly. Note any disagreement between agents."
    )
    decision = _ask(
        Decision,
        "You are the head trader coordinating three analysts. Weigh their views, "
        "favour agreement, and be honest about uncertainty. A weak or conflicted "
        "setup should be WAIT, not a forced trade. You are advising a human who makes "
        "the final call; never overstate confidence.",
        human,
        kind="decision",
    )
    return {"decision": decision}


# --- deterministic risk gate (final authority) ------------------------------

def risk_gate_node(state: AnalystState) -> dict:
    """Pure rules. Sizes the position and can VETO the LLM's decision.

    - position size from fixed-fractional risk and an ATR-based stop.
    - veto to WAIT if confidence too low, regime is high_vol with low conviction,
      or the stop distance is degenerate.
    """
    d = state["decision"]
    facts = state["facts"]
    equity = state.get("account_equity", 10_000.0)
    risk_frac = state.get("risk_per_trade", 0.005)  # 0.5% per trade

    last = facts["last_price"]
    atr = facts["atr14"] or 0.0
    reasons: list[str] = []
    vetoed = False
    action = d.action

    MIN_CONF = 0.55
    if d.action != "WAIT" and d.confidence < MIN_CONF:
        vetoed, action = True, "WAIT"
        reasons.append(f"Decision confidence {d.confidence:.2f} < {MIN_CONF} threshold.")

    if state["regime"].regime == "high_vol" and d.confidence < 0.7:
        vetoed, action = True, "WAIT"
        reasons.append("High-volatility regime without strong conviction -> stand aside.")

    stop_distance = 2.0 * atr  # 2-ATR stop
    if action != "WAIT" and stop_distance <= 0:
        vetoed, action = True, "WAIT"
        reasons.append("ATR is zero/degenerate; cannot size a stop safely.")

    if action == "BUY":
        stop_price = last - stop_distance
    elif action == "SELL":
        stop_price = last + stop_distance
    else:
        stop_price = 0.0

    if action != "WAIT" and stop_distance > 0:
        risk_amount = equity * risk_frac
        max_units = risk_amount / stop_distance
        reasons.append(
            f"Risking {risk_frac:.1%} of {equity:,.0f} = {risk_amount:,.0f}; "
            f"2-ATR stop = {stop_distance:.5f} -> max {max_units:,.0f} units."
        )
    else:
        max_units = 0.0
        if action == "WAIT" and not reasons:
            reasons.append("Decision is WAIT; no position to size.")

    return {"risk": RiskAssessment(
        final_action=action, vetoed=vetoed,
        max_position_units=round(max_units, 2), stop_price=round(stop_price, 5),
        reasons=reasons,
    )}
