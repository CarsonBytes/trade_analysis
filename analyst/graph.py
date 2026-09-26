"""Wire the agents into a LangGraph StateGraph.

Flow: facts -> analysts -> decision -> risk_gate -> END
The analysts node collapses regime + technical + sentiment into ONE LLM call
(ADDED 2026-09-26), saving ~2K input tokens/instrument and ~66% latency.
The three views are independent, so batching in one structured output is
lossless.
"""
from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from .state import AnalystState
from .nodes import (
    analysts_node, decision_node, risk_gate_node,
)


def gather_facts_node(state: AnalystState) -> dict:
    # facts/facts_text/news are populated by the caller before invoke; this node
    # is just the fan-out anchor.
    return {}


def build_graph():
    g = StateGraph(AnalystState)
    g.add_node("facts", gather_facts_node)
    g.add_node("analysts", analysts_node)
    g.add_node("decision", decision_node)
    g.add_node("risk_gate", risk_gate_node)

    g.add_edge(START, "facts")
    g.add_edge("facts", "analysts")
    g.add_edge("analysts", "decision")
    g.add_edge("decision", "risk_gate")
    g.add_edge("risk_gate", END)
    return g.compile()
