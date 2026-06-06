"""Wire the agents into a LangGraph StateGraph.

Flow: facts -> (regime || technical || sentiment) -> decision -> risk_gate -> END
The three analysts fan out in parallel (each writes a distinct state key, so
there's no merge conflict) and fan back in at the decision node.
"""
from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from .state import AnalystState
from .nodes import (
    regime_node, technical_node, sentiment_node, decision_node, risk_gate_node,
)


def gather_facts_node(state: AnalystState) -> dict:
    # facts/facts_text/news are populated by the caller before invoke; this node
    # is just the fan-out anchor so the three analysts can run in parallel.
    return {}


def build_graph():
    g = StateGraph(AnalystState)
    g.add_node("facts", gather_facts_node)
    g.add_node("regime", regime_node)
    g.add_node("technical", technical_node)
    g.add_node("sentiment", sentiment_node)
    g.add_node("decision", decision_node)
    g.add_node("risk_gate", risk_gate_node)

    g.add_edge(START, "facts")
    # fan-out
    g.add_edge("facts", "regime")
    g.add_edge("facts", "technical")
    g.add_edge("facts", "sentiment")
    # fan-in: decision waits for all three
    g.add_edge("regime", "decision")
    g.add_edge("technical", "decision")
    g.add_edge("sentiment", "decision")
    # final authority
    g.add_edge("decision", "risk_gate")
    g.add_edge("risk_gate", END)
    return g.compile()
