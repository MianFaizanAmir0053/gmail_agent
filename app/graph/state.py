"""Graph state.

Kept deliberately small. The state is checkpointed to Postgres after every node,
so anything stored here is written, read back, and carried across restarts --
which is a cost, and a compatibility surface once rows outlive a deploy. If a
value can be recomputed or looked up, it does not belong here.

The one thing that *must* be here is everything the approval card needs to
render. The Telegram handler in M06 resumes a thread it did not start, possibly
hours later in a different process, and should not have to re-query the graph
to draw a message.
"""

from __future__ import annotations

from typing import TypedDict

from app.contracts import ActionResult, EmailMessage, ExtractionResult


class GraphState(TypedDict, total=False):
    message_id: str
    """Gmail message ID. Also the ledger key and the graph's `thread_id` --
    one identifier threading the whole system, so a row in any table can be
    traced to a checkpoint and back."""

    thread_id: str
    """Gmail *conversation* thread, not the LangGraph thread. Distinct concepts
    that unfortunately share a name."""

    email: EmailMessage
    extraction: ExtractionResult

    conflicts: list[str]
    """Human-readable clashes found by the free/busy check, for the approval card."""

    approved: bool
    correction: str
    """Free-text correction from a human choosing Edit; fed back into extraction."""

    revisions: int
    """Edit rounds so far. Bounded in the graph, never in the prompt."""

    action: ActionResult
    error: str
