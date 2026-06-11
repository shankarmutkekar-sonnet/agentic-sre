"""
graph.py — LangGraph StateGraph definition for the SRE Investigation Agent.

Execution flow:
                         ┌─────────────┐
                         │ fetch_alarm │  (entry — sequential)
                         └──────┬──────┘
        ┌──────────────┬────────┼────────┬──────────────────┐
        ▼              ▼        ▼        ▼                  ▼
 fetch_metrics   fetch_logs  fetch_  fetch_github   fetch_argocd
   (parallel)    (parallel) cloudtrail (parallel)    (parallel)
        └──────────────┴────────┼────────┴──────────────────┘
                                ▼
                          synthesize  (fan-in — waits for all 5)
                                │
                               END

Parallel execution:
  LangGraph executes nodes that share the same "super-step" concurrently
  when the graph is run with ainvoke / astream and all node functions are
  async. The four investigation nodes fan out from fetch_alarm and fan back
  in at synthesize — LangGraph merges their state updates before synthesize
  is called.

  List fields that are written by multiple parallel nodes (observations,
  investigation_gaps) use operator.add reducers defined in state.py so that
  updates are concatenated rather than overwritten.
"""

from langgraph.graph import END, StateGraph

from agent.state import InvestigationState
from agent.nodes import (
    fetch_alarm,
    fetch_argocd,
    fetch_cloudtrail,
    fetch_github,
    fetch_logs,
    fetch_metrics,
    synthesize,
)


def build_graph():
    """
    Compile and return the investigation StateGraph.

    Call once at module level (outside the Lambda handler) so the compiled
    graph is reused across warm invocations — compilation is expensive.
    """
    graph = StateGraph(InvestigationState)

    # ── Register nodes ────────────────────────────────────────────────────────
    graph.add_node("fetch_alarm",      fetch_alarm.run)
    graph.add_node("fetch_metrics",    fetch_metrics.run)
    graph.add_node("fetch_logs",       fetch_logs.run)
    graph.add_node("fetch_cloudtrail", fetch_cloudtrail.run)
    graph.add_node("fetch_github",     fetch_github.run)
    graph.add_node("fetch_argocd",     fetch_argocd.run)
    graph.add_node("synthesize",       synthesize.run)

    # ── Entry point ───────────────────────────────────────────────────────────
    graph.set_entry_point("fetch_alarm")

    # ── Fan-out: fetch_alarm → all five parallel investigation nodes ──────────
    graph.add_edge("fetch_alarm", "fetch_metrics")
    graph.add_edge("fetch_alarm", "fetch_logs")
    graph.add_edge("fetch_alarm", "fetch_cloudtrail")
    graph.add_edge("fetch_alarm", "fetch_github")
    graph.add_edge("fetch_alarm", "fetch_argocd")

    # ── Fan-in: all five parallel nodes → synthesize ──────────────────────────
    graph.add_edge("fetch_metrics",    "synthesize")
    graph.add_edge("fetch_logs",       "synthesize")
    graph.add_edge("fetch_cloudtrail", "synthesize")
    graph.add_edge("fetch_github",     "synthesize")
    graph.add_edge("fetch_argocd",     "synthesize")

    # ── Terminal edge ─────────────────────────────────────────────────────────
    graph.add_edge("synthesize", END)

    return graph.compile()


# Compile once at import time — reused across Lambda warm starts
investigation_graph = build_graph()
