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

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from langgraph.types import interrupt

from app.contracts import ActionResult, ExtractionResult
from app.extraction.pipeline import ExtractionPipeline
from app.google.calendar import CalendarClient
from app.google.gmail import GmailClient
from app.graph.state import GraphState
from app.graph.versioning import action_type
from app.policy.hashing import event_args, tool_for
from app.policy.participants import named, outside, participants
from app.policy.registry import Approval, Registry
from app.store.ledger import MessageLedger, MessageStatus
from app.tools.calendar_tool import check_conflicts

log = logging.getLogger(__name__)

MAX_REVISIONS = 2
"""Edit rounds before the graph gives up and asks for a decision.

Enforced here rather than in the prompt: an instruction to "only revise twice"
is a suggestion, a counter in graph state is a guarantee.
"""

CARRIED_A_CODE = "carried a sign-in code"
"""Ledger reason for credential mail (M18, decision 2), recorded before any
model reads it. Fixed: it quotes nothing of the message."""

NO_START_TIME = "a meeting with no start time"
"""Ledger reason for a meeting the graph could not place (M18, D7): a fixed
phrase, as for "not a meeting", in place of the model's reasoning."""

NOT_A_MEETING = "not a meeting"
"""Ledger reason when the model finds no meeting (M20, D4).

A fixed phrase in place of the model's reasoning, which can quote the email.
Since M20 feeds read mail as well as unread, one-time-code and password-reset
mail in Primary reaches the classifier, and its reasoning must not reach the
ledger.
"""

SWEEP_REASON = "swept: observe mode ended"
"""Ledger reason for proposals cleared in bulk (M15).

Kept distinct from "declined by user": M24 counts a person's cancellations
against the agent when deciding what it may do unattended, and a sweep says
nothing about whether the proposal was any good.
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
    # The owner's correction, from an Edit, goes to the pipeline as it is: the
    # pipeline puts it in the system instruction, where no email can write
    # (M18, D3). With it goes the proposal on the card, which the checkpoint
    # still holds, so the Edit changes that proposal: what the owner did not
    # mention, the title among it, is kept rather than guessed again.
    correction = state.get("correction", "")
    extraction = deps.pipeline.extract(
        state["email"],
        now_utc=deps.now(),
        user_timezone=deps.user_timezone,
        correction=correction,
        current=state.get("extraction") if correction else None,
    )
    return {"extraction": extraction}


def detect_conflicts(deps: Deps, state: GraphState) -> GraphState:
    extraction = state["extraction"]
    if extraction.start_utc is None or extraction.end_utc is None:
        return {"conflicts": [], "outside_guests": [], "thread_guests": [], "guest_sources": {}}

    args = event_args(extraction, state["message_id"])
    check = check_conflicts(deps.calendar, args)
    outside_guests = _outside_guests(deps, state, args.attendees)
    # Where each guest came from, computed once, here, and read by every card
    # (M18, D5). Allowed contacts are applied where a card is drawn.
    sources, quoted = named(args.attendees, state["email"])
    return {
        "conflicts": [check.describe()] if check.has_conflict else [],
        "outside_guests": outside_guests,
        "thread_guests": [guest for guest in args.attendees if guest not in outside_guests],
        "guest_sources": dict(sources),
        "quoted_section": quoted,
    }


def _outside_guests(deps: Deps, state: GraphState, guests: list[str]) -> list[str]:
    """Guests not in the email's own Gmail thread (M17, D4).

    If the thread cannot be read, every guest is outside: the proposal still
    parks, and the owner can Allow. A Gmail outage never fails a re-park.
    """
    if not guests:
        return []
    try:
        thread = deps.gmail.thread_headers(state["email"].thread_id)
    except Exception as exc:
        log.warning(
            "could not read the thread of %s (%s); every guest is outside",
            state["message_id"],
            type(exc).__name__,
        )
        return list(guests)
    return outside(guests, participants=participants(thread), confirmed=frozenset())


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
            # Marked on the card, and checked before any Confirm (M17, D4).
            "outside_guests": state.get("outside_guests", []),
            # Where each guest came from (M18, D5).
            "thread_guests": state.get("thread_guests", []),
            "guest_sources": state.get("guest_sources", {}),
            "quoted_section": state.get("quoted_section", False),
            # `DRY_RUN` is read when a session is built, not stored with the
            # thread. Recording it here is what lets M17 refuse a proposal that
            # was parked under a different setting than the one it would run in.
            "dry_run": deps.calendar.dry_run,
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
        # Why an operator or the worker swept it, when it says (M17, D2).
        "sweep_reason": str(decision.get("reason") or "") if action == "sweep" else "",
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
    email = state.get("email")
    extraction = state.get("extraction")
    if email is not None and email.credential:
        # Set aside straight after the fetch: no model has read it.
        reason = CARRIED_A_CODE
    elif extraction is None or not extraction.is_meeting:
        # The model's reasoning stays out of the ledger: it can quote the email.
        reason = NOT_A_MEETING
    else:
        # A meeting the graph could not place. Its reasoning stays in the
        # checkpoint, which the purge removes; the ledger gets the fixed words.
        reason = NO_START_TIME
    deps.ledger.mark(state["message_id"], MessageStatus.SKIPPED, error=reason)
    return {"action": ActionResult(status="skipped_duplicate", error=reason)}


def reject(deps: Deps, state: GraphState) -> GraphState:
    """Reached from two directions: a human declining, or an operator or the
    worker sweeping parked proposals.

    The reason is recorded rather than assumed, so the failures view does not
    report an operator's decision as a person's.
    """
    swept = state.get("swept")
    reason = (state.get("sweep_reason") or SWEEP_REASON) if swept else "declined by user"
    deps.ledger.mark(state["message_id"], MessageStatus.REJECTED, error=reason)
    return {"action": ActionResult(status="rejected", error=reason)}
