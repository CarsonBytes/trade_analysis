"""Deterministic trend-strength scoring -- the cheap, always-on 'obvious trend'
finder. Runs across every instrument with ZERO LLM calls, so it can refresh as
often as you like. The expensive LLM board-scan only deep-dives the top of this
ranking.

'Obvious' = multi-timeframe trend agreement + momentum confirmation, with RSI
used to flag when a trend is stretched (overbought/oversold).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import net  # noqa: F401
from analyst.features import compute_facts  # from quant/


@dataclass
class Score:
    key: str
    direction: str       # "long" | "short" | "neutral"
    strength: int        # 1..5 (5 = strongest, most obvious)
    obviousness: float   # sortable score
    signal: str          # "BUY" | "SELL" | "WATCH"
    note: str
    facts: dict
    facts_text: str


def score_from_facts(key: str, facts: dict, facts_text: str) -> Score:
    t = facts["trend"]
    votes = sum({"up": 1, "down": -1}.get(t[k], 0) for k in ("short", "medium", "long"))
    mom20 = facts["returns"].get("20d") or 0.0
    rsi = facts["rsi14"]

    direction = "long" if votes > 0 else ("short" if votes < 0 else "neutral")

    # momentum agrees with trend?
    mom_agrees = (votes > 0 and mom20 > 0) or (votes < 0 and mom20 < 0)
    # obviousness: alignment is primary, momentum magnitude is the tie-breaker
    obviousness = abs(votes) + min(abs(mom20) * 20, 1.0) + (0.5 if mom_agrees else 0.0)

    strength = max(1, min(5, abs(votes) + (1 if mom_agrees else 0) +
                          (1 if abs(mom20) > 0.03 else 0)))

    # signal: only call BUY/SELL when alignment is strong AND momentum confirms
    signal = "WATCH"
    if votes >= 2 and mom20 > 0:
        signal = "BUY"
    elif votes <= -2 and mom20 < 0:
        signal = "SELL"

    notes = []
    notes.append(f"{abs(votes)}/3 timeframes {direction}")
    notes.append(f"20d {mom20:+.1%}")
    if rsi >= 70:
        notes.append(f"RSI {rsi:.0f} overbought (stretched)")
    elif rsi <= 30:
        notes.append(f"RSI {rsi:.0f} oversold (stretched)")
    if signal in ("BUY", "SELL") and (rsi >= 70 or rsi <= 30):
        notes.append("trend strong but extended -- watch for pullback")

    return Score(
        key=key, direction=direction, strength=strength, obviousness=round(obviousness, 3),
        signal=signal, note="; ".join(notes), facts=facts, facts_text=facts_text,
    )


def rank(scores: list[Score]) -> list[Score]:
    """Most obvious setups first."""
    return sorted(scores, key=lambda s: s.obviousness, reverse=True)
