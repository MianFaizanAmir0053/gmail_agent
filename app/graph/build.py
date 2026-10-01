"""Graph construction.

    fetch -> classify -> extract -> review -> conflicts -> await_approval -> act
                  |                              ^              |
                  v                              |              +-> reject
                 skip                            +-- edit ------+

Two things here are load-bearing:

**The checkpointer must be Postgres.** An approval may be answered hours later,
after a redeploy, in a different process. `MemorySaver` would lose the thread
and the resume would have nothing to attach to.

**`thread_id` is the Gmail message ID.** It is already the ledger's primary key,
so one identifier connects a checkpoint, a ledger row, and a Telegram callback
without a translation table.
"""

from __future__ import annotations

from functools import partial
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.graph import nodes
from app.graph.nodes import MAX_REVIEW_ROUNDS, MAX_REVISIONS, Deps
from app.graph.state import GraphState
from app.obs.trace import Tracer, traced

NETWORK_RETRY = RetryPolicy(max_attempts=3, initial_interval=1.0, backoff_factor=2.0)
"""For nodes that cross the network. Gmail and Gemini both rate-limit, and a
429 that clears in twenty seconds should not dead-letter a message.

Deliberately not applied to `act`. A write interrupted halfway is finished by
the worker's next attempt, from what the registry stored (M17, D3), and that
attempt is counted and spaced out like any other. A retry inside the node
would spend those attempts in seconds.
"""


def _is_meeting(state: GraphState) -> Literal["extract", "skip"]:
    extraction = state.get("extraction")
    return "skip" if extraction is not None and not extraction.is_meeting else "extract"


def _has_event(state: GraphState) -> Literal["conflicts", "skip"]:
    extraction = state.get("extraction")
    if extraction is None or not extraction.is_meeting or extraction.start_utc is None:
        return "skip"
    return "conflicts"


def _after_review(state: GraphState) -> Literal["extract", "conflicts", "skip", "reject"]:
    """Where the reviewer's verdict sends the graph.

    The cap lives here rather than in the reviewer's prompt. "Only revise twice"
    is a request; a counter compared in the router is the reason the loop
    terminates. Once the budget is spent the graph carries on with whatever the
    last extraction produced -- a human is about to see it either way, and the
    reviewer's objections travel with it onto the approval card.
    """
    decision = state.get("review_decision", "approve")

    if decision == "reject":
        return "reject"
    if decision == "revise" and state.get("review_rounds", 0) <= MAX_REVIEW_ROUNDS:
        return "extract"
    return _has_event(state)


def _decision(state: GraphState) -> Literal["act", "reject", "extract"]:
    if state.get("correction") and state.get("revisions", 0) <= MAX_REVISIONS:
        return "extract"
    if state.get("approved"):
        return "act"
    return "reject"


def build_graph(
    deps: Deps, checkpointer: BaseCheckpointSaver[Any], tracer: Tracer | None = None
) -> Any:
    builder: StateGraph[GraphState, Any, Any, Any] = StateGraph(GraphState)

    def node(name: str, fn: Any) -> Any:
        """Bind deps, then wrap for tracing.

        Wrapping here rather than instrumenting each node body means a new node
        is traced by construction -- and a node that is never traced looks
        exactly like one that was fast and free.
        """
        return traced(tracer, name, partial(fn, deps))

    # No retry policy of its own (M20, D3): the Gmail client already retries
    # a 429 or a 5xx, for at most 30 seconds in all, and retrying that here
    # would multiply it towards the platform's kill timeout. A message gone
    # before its turn (`MessageGoneError`) is final either way.
    builder.add_node("fetch", node("fetch", nodes.fetch))
    builder.add_node("classify", node("classify", nodes.classify), retry_policy=NETWORK_RETRY)
    builder.add_node("extract", node("extract", nodes.extract), retry_policy=NETWORK_RETRY)
    builder.add_node("review", node("review", nodes.review))
    builder.add_node(
        "conflicts", node("conflicts", nodes.detect_conflicts), retry_policy=NETWORK_RETRY
    )
    builder.add_node("await_approval", node("await_approval", nodes.await_approval))
    builder.add_node("act", node("act", nodes.act))
    builder.add_node("skip", node("skip", nodes.skip))
    builder.add_node("reject", node("reject", nodes.reject))

    builder.add_edge(START, "fetch")
    builder.add_edge("fetch", "classify")
    builder.add_conditional_edges("classify", _is_meeting, ["extract", "skip"])
    builder.add_edge("extract", "review")
    builder.add_conditional_edges(
        "review", _after_review, ["extract", "conflicts", "skip", "reject"]
    )
    builder.add_edge("conflicts", "await_approval")
    builder.add_conditional_edges("await_approval", _decision, ["act", "reject", "extract"])
    builder.add_edge("act", END)
    builder.add_edge("skip", END)
    builder.add_edge("reject", END)

    return builder.compile(checkpointer=checkpointer)
