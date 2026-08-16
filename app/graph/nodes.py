"""Graph nodes.

One module rather than the planned file-per-node: seven functions of a few lines
each, and splitting them would mean seven imports to follow one flow.

Nodes stay thin on purpose. Every one of them delegates to `app.extraction`,
`app.tools`, or `app.store`, all of which are unit-testable without a graph.
A node's job is to move state, not to hold logic.

**Transient failures are not caught here.** A rate limit or a timeout should hit
the node's `retry_policy` and be retried; swallowing it into `state["error"]`
would turn a recoverable blip into a dead letter. Only terminal outcomes are
turned into state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from langgraph.types import interrupt

from app.contracts import ActionResult, ExtractionResult
from app.extraction.pipeline import ExtractionPipeline
from app.google.calendar import CalendarClient
from app.google.gmail import GmailClient
from app.graph.state import GraphState
from app.store.ledger import MessageLedger, MessageStatus
from app.tools.calendar_tool import CreateEventInput, check_conflicts, execute_create_event

MAX_REVISIONS = 2
"""Edit rounds before the graph gives up and asks for a decision.

Enforced here rather than in the prompt: an instruction to "only revise twice"
is a suggestion, a counter in graph state is a guarantee.
"""


@dataclass(slots=True)
class Deps:
    """Everything the nodes need, injected so tests can supply fakes."""

    gmail: GmailClient
    pipeline: ExtractionPipeline
    calendar: CalendarClient
    ledger: MessageLedger
    user_timezone: str

    def now(self) -> datetime:
        return datetime.now(UTC)


def fetch(deps: Deps, state: GraphState) -> GraphState:
    return {"email": deps.gmail.get_message(state["message_id"])}


def classify(deps: Deps, state: GraphState) -> GraphState:
    verdict = deps.pipeline.classify(
        state["email"], now_utc=deps.now(), user_timezone=deps.user_timezone
    )
    if verdict.is_meeting:
        return {}

    # Record the verdict even though nothing will be booked -- the reasoning is
    # what makes a false negative debuggable later.
    return {
        "extraction": ExtractionResult(
            is_meeting=False, confidence=verdict.confidence, reasoning=verdict.reasoning
        )
    }


def extract(deps: Deps, state: GraphState) -> GraphState:
    extraction = deps.pipeline.extract(
        state["email"],
        now_utc=deps.now(),
        user_timezone=deps.user_timezone,
        extra=state.get("correction", ""),
    )
    return {"extraction": extraction}


def review(deps: Deps, state: GraphState) -> GraphState:
    """Placeholder for M13's reviewer agent.

    Wired now so adding the reviewer is a one-node change rather than a graph
    rewrite -- and so the shape of the flow in the README does not change later.
    """
    return {}


def detect_conflicts(deps: Deps, state: GraphState) -> GraphState:
    extraction = state["extraction"]
    if extraction.start_utc is None or extraction.end_utc is None:
        return {"conflicts": []}

    check = check_conflicts(deps.calendar, _to_tool_input(state))
    return {"conflicts": [check.describe()] if check.has_conflict else []}


def await_approval(deps: Deps, state: GraphState) -> GraphState:
    """Suspend until a human decides.

    `interrupt` persists the checkpoint and stops. The process can exit, deploy,
    and come back; the resume arrives via `Command(resume=...)` against the same
    thread_id. This is the whole reason the checkpointer must be Postgres and
    not in-memory.
    """
    decision = interrupt(
        {
            "message_id": state["message_id"],
            "proposed": state["extraction"].model_dump(mode="json"),
            "conflicts": state.get("conflicts", []),
        }
    )
    action = str(decision.get("action", "cancel")).lower()

    if action == "edit":
        return {
            "correction": str(decision.get("correction", "")),
            "revisions": state.get("revisions", 0) + 1,
        }
    return {"approved": action == "confirm"}


def act(deps: Deps, state: GraphState) -> GraphState:
    result = execute_create_event(deps.calendar, _to_tool_input(state))

    if result.status == "created" and result.event_id:
        deps.ledger.mark(
            state["message_id"], MessageStatus.CREATED, calendar_event_id=result.event_id
        )
    else:
        # dry_run and failed both land here. Neither may claim CREATED: the
        # ledger's CHECK constraint requires an event id for that status, and
        # inventing one would corrupt the audit trail.
        deps.ledger.mark(
            state["message_id"],
            MessageStatus.SKIPPED if result.status == "dry_run" else MessageStatus.FAILED,
            error=result.error or result.status,
        )
    return {"action": result}


def skip(deps: Deps, state: GraphState) -> GraphState:
    reason = state["extraction"].reasoning if "extraction" in state else "not a meeting"
    deps.ledger.mark(state["message_id"], MessageStatus.SKIPPED, error=reason)
    return {"action": ActionResult(status="skipped_duplicate", error=reason)}


def reject(deps: Deps, state: GraphState) -> GraphState:
    deps.ledger.mark(state["message_id"], MessageStatus.REJECTED, error="declined by user")
    return {"action": ActionResult(status="rejected", error="declined by user")}


def _to_tool_input(state: GraphState) -> CreateEventInput:
    extraction = state["extraction"]
    assert extraction.start_utc is not None
    assert extraction.end_utc is not None
    return CreateEventInput(
        title=extraction.title or "(untitled)",
        start_utc=extraction.start_utc,
        end_utc=extraction.end_utc,
        timezone=extraction.timezone or "UTC",
        attendees=extraction.attendees,
        location=extraction.location,
        description=f"Created by mailagent from message {state['message_id']}.",
    )
