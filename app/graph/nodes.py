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

from app.agents.reviewer import Reviewer
from app.contracts import ActionResult, ExtractionResult
from app.extraction.pipeline import ExtractionPipeline
from app.google.calendar import CalendarClient
from app.google.gmail import GmailClient
from app.graph.state import GraphState
from app.graph.versioning import action_type
from app.policy.hashing import event_args, tool_for
from app.policy.registry import Approval, Registry
from app.store.ledger import MessageLedger, MessageStatus
from app.tools.calendar_tool import check_conflicts

MAX_REVISIONS = 2
"""Edit rounds before the graph gives up and asks for a decision.

Enforced here rather than in the prompt: an instruction to "only revise twice"
is a suggestion, a counter in graph state is a guarantee.
"""

SWEEP_REASON = "swept: observe mode ended"
"""Ledger reason for proposals cleared in bulk (M15).

Kept distinct from "declined by user": M24 counts a person's cancellations
against the agent when deciding what it may do unattended, and a sweep says
nothing about whether the proposal was any good.
"""

MAX_REVIEW_ROUNDS = 2
"""Reviewer-driven re-extractions before the graph stops listening.

A separate budget from `MAX_REVISIONS`. The failure they guard against is the
same -- an unbounded loop between two components that keep disagreeing -- but a
human asking twice and an agent asking twice should not exhaust each other's
allowance.
"""


@dataclass(slots=True)
class Deps:
    """Everything the nodes need, injected so tests can supply fakes."""

    gmail: GmailClient
    pipeline: ExtractionPipeline
    calendar: CalendarClient
    ledger: MessageLedger
    user_timezone: str
    registry: Registry
    """The only way `act` writes (M17, D1). It checks the approval again and
    finishes a write an earlier attempt began."""
    reviewer: Reviewer | None = None
    """M13. Left unset, `review` approves everything and the graph behaves
    exactly as it did before the reviewer existed."""
    pipeline_version: str = "unversioned"
    """What shaped this session's proposals (`app/graph/versioning.py`).
    Computed once per session from settings and prompts."""
    args_key: bytes = b"unkeyed: tests only"
    """Keys the hash an approval binds (`app/policy/hashing.py`). Derived from
    `FERNET_KEY` by `graph_session`; the default serves fakes in tests."""

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
        extra=_guidance(state),
    )
    return {"extraction": extraction}


def _guidance(state: GraphState) -> str:
    """Human correction and reviewer feedback, both labelled.

    Concatenated rather than merged, and the human is named first: on a
    disagreement the extractor should know which instruction came from the
    person who will be asked to approve the result.
    """
    parts: list[str] = []
    if correction := state.get("correction", ""):
        parts.append(f"Correction from the user, which takes precedence:\n{correction}")
    if feedback := state.get("review_feedback", ""):
        parts.append(f"A reviewer found these problems with your previous answer:\n{feedback}")
    return "\n\n".join(parts)


def review(deps: Deps, state: GraphState) -> GraphState:
    """Second agent, own tools, own evidence.

    Returns a decision rather than acting on one: the routing lives in
    `build.py`, where the revision cap is enforced. A node that decided its own
    successor could loop for ever no matter what the counter said.
    """
    if deps.reviewer is None:
        return {"review_decision": "approve"}

    extraction = state["extraction"]
    if not extraction.is_meeting:
        # Nothing to review. Spending a call to confirm that a newsletter is
        # still not a meeting is the reviewer's cheapest way to be useless.
        return {"review_decision": "approve"}

    verdict = deps.reviewer(
        state["email"], extraction, now_utc=deps.now(), user_timezone=deps.user_timezone
    )

    return {
        "review_decision": verdict.decision,
        "review_issues": verdict.issues,
        "review_feedback": verdict.feedback() if verdict.decision == "revise" else "",
        "review_rounds": state.get("review_rounds", 0) + (verdict.decision == "revise"),
    }


def detect_conflicts(deps: Deps, state: GraphState) -> GraphState:
    extraction = state["extraction"]
    if extraction.start_utc is None or extraction.end_utc is None:
        return {"conflicts": []}

    check = check_conflicts(deps.calendar, event_args(extraction, state["message_id"]))
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
            # `DRY_RUN` is read when a session is built, not stored with the
            # thread. Recording it here is what lets M17 refuse a proposal that
            # was parked under a different setting than the one it would run in.
            "dry_run": deps.calendar.dry_run,
            # The reviewer's last objections, for the card: a human should see
            # the second opinion, not only its effect.
            "review_issues": state.get("review_issues", []),
            # Recomputed at every park, so an edit that adds a guest turns a
            # hold into an invite. The revision is not here: it is read from
            # the thread's state, which proposals parked before M16 also have.
            "action_type": action_type(state["extraction"].attendees),
            "pipeline_version": deps.pipeline_version,
        }
    )
    action = str(decision.get("action", "cancel")).lower()

    if action == "edit":
        return {
            "correction": str(decision.get("correction", "")),
            "revisions": state.get("revisions", 0) + 1,
        }
    # Any other answer ends the edit round. Left in place, the previous
    # correction outlives it: `_decision` would still see an edit in progress
    # and send a Confirm or Cancel straight back to `extract`.
    return {
        "approved": action == "confirm",
        "correction": "",
        "swept": action == "sweep",
        # What `decide()` recorded for a Confirm (M17, D2). Without it, `act`
        # refuses: a Confirm from before M17 never runs unchecked.
        "approval": decision.get("approval") if action == "confirm" else None,
    }


def act(deps: Deps, state: GraphState) -> GraphState:
    """Run the approved write through the registry (M17, D1).

    The registry checks the approval again, then writes, or finishes a write
    an earlier attempt began. A refusal is final: the message is FAILED with
    the registry's reason, and nothing is retried.
    """
    message_id = state["message_id"]
    args = event_args(state["extraction"], message_id)
    outcome = deps.registry.execute(
        tool_for(args.attendees),
        args,
        approval=Approval.parse(state.get("approval")),
        message_id=message_id,
    )

    if outcome.status == "created":
        assert outcome.event_id is not None
        deps.ledger.mark(message_id, MessageStatus.CREATED, calendar_event_id=outcome.event_id)
        return {"action": ActionResult(status="created", event_id=outcome.event_id)}
    # Neither of the others may claim CREATED: the ledger's CHECK constraint
    # requires an event id for that status, and inventing one would corrupt
    # the audit trail.
    if outcome.status == "dry_run":
        deps.ledger.mark(message_id, MessageStatus.SKIPPED, error="dry_run")
        return {"action": ActionResult(status="dry_run")}
    deps.ledger.mark(message_id, MessageStatus.FAILED, error=outcome.reason)
    return {"action": ActionResult(status="failed", error=outcome.reason)}


def skip(deps: Deps, state: GraphState) -> GraphState:
    reason = state["extraction"].reasoning if "extraction" in state else "not a meeting"
    deps.ledger.mark(state["message_id"], MessageStatus.SKIPPED, error=reason)
    return {"action": ActionResult(status="skipped_duplicate", error=reason)}


def reject(deps: Deps, state: GraphState) -> GraphState:
    """Reached from three directions: a human declining, the reviewer
    rejecting, or an operator sweeping parked proposals.

    The reason is recorded rather than assumed, so the failures view does not
    report an agent's or an operator's decision as a person's.
    """
    if state.get("review_decision") == "reject":
        reason = "; ".join(state.get("review_issues", [])) or "rejected by reviewer"
    elif state.get("swept"):
        reason = SWEEP_REASON
    else:
        reason = "declined by user"
    deps.ledger.mark(state["message_id"], MessageStatus.REJECTED, error=reason)
    return {"action": ActionResult(status="rejected", error=reason)}
