"""Applying queued decisions (M16, D1).

This is the only code that resumes a thread. Channels record decisions with
`decide()` and return; the worker takes each open decision and moves it one
step at a time from what is stored -- the thread's checkpoint and the ledger
-- never from what it remembers. After a crash the decision is still open,
and the next pass reads the state again and carries on: a decision already
applied is recognised from that state and never applied twice.

| Stored state                                   | Step                          |
|------------------------------------------------|-------------------------------|
| Parked at the decision's revision              | resume                        |
| Parked at the next revision, after an edit     | settle: reparked              |
| Ledger final                                   | settle: that status           |
| Stopped mid-graph, `act` not next              | re-drive                      |
| Stopped before `act`, with an approval         | re-drive (M17, D3)            |
| Stopped before `act`, with none                | settle: failed, never re-run  |
| Anything else                                  | settle: failed                |

A settle is one transaction, and each write in it is conditional on the
state it expects, so settling twice changes nothing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Literal

import psycopg

from app.channel.park import (
    Announce,
    ProposalRecord,
    proposal_from,
    require_transaction,
    write_park,
)
from app.graph.runner import GraphSession, ThreadView
from app.policy import audit, control
from app.policy.hashing import Binding
from app.policy.registry import Approval, PausedError
from app.store.ledger import TERMINAL_STATUSES, MessageLedger, MessageStatus

log = logging.getLogger(__name__)

ACT_INTERRUPTED = "act interrupted"
"""The thread stopped with `act` next and no approval to finish it from: a
Confirm from before M17. It is never run again. A Confirm with an approval is
re-driven instead, whatever its action's status: the registry checks a new
action, finishes a begun one from what it stored, and returns a settled one's
outcome (M17, D3)."""

UNEXPECTED_REVISION = "unexpected revision"
NO_OUTCOME = "no outcome recorded"
ATTEMPTS_EXHAUSTED = "attempts exhausted"

MAX_PASSES = 3
"""Resume or re-drive, then settle, takes two passes. A third means a step
made no progress, which is reported rather than retried in a loop."""

RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=10))
"""How long to wait after the first and second failed attempts. Long enough
for a rate limit or a brief outage to clear, short enough that the owner is
not left wondering for an afternoon."""

MAX_ATTEMPTS = len(RETRY_DELAYS) + 1

UNCONFIRMED_RETRY = timedelta(hours=1)
"""How often a decision whose calendar write could not be confirmed asks
Google again, once its attempts are spent (M17, D3)."""

LOOKUP_DELAY = timedelta(minutes=10)
"""How long after its last failed write an action is looked up. Google can
finish an insert after the call timed out on this side; asked at once, it
could answer "no such event" for one about to exist."""

_REGISTRY_REASONS = frozenset(audit.REASONS.values())

LEASE = timedelta(minutes=30)
"""How long a worker owns a decision. Only one worker runs (a single machine,
`max_instances=1`), so the lease guards against a mistake -- a second machine,
a local run against the production database -- rather than a normal case. A
worker that dies holding one only delays the decision until it expires.

Longer than any resume should take, because it cannot be renewed while one
runs, and the model calls inside a resume have no deadline of their own."""


@dataclass(frozen=True, slots=True)
class OpenDecision:
    id: int
    message_id: str
    revision: int
    action: str
    correction: str | None
    attempts: int
    action_id: int | None = None
    """A Confirm's approval (M17, D2). None for other decisions, and for a
    Confirm left open by M16."""
    nonce: str | None = field(default=None, repr=False)
    action_status: str | None = None


@dataclass(frozen=True, slots=True)
class Step:
    kind: Literal["resume", "redrive", "settle"]
    outcome: str | None = None
    final_status: str | None = None
    reason: str | None = None


def step_for(
    view: ThreadView,
    ledger_status: MessageStatus | None,
    decision: OpenDecision,
    *,
    ledger_error: str | None = None,
) -> Step:
    """The next step for an open decision, from stored state alone.

    `ledger_error` is the ledger's reason. When the registry refused an
    action, `act` marked the message FAILED with one of its fixed phrases,
    and the decision settles with that phrase rather than a generic one.
    """
    if view.parked:
        if view.revision == decision.revision:
            return Step("resume")
        if decision.action == "edit" and view.revision == decision.revision + 1:
            return Step("settle", outcome="reparked")
        # Something else moved the thread. It is still awaiting the owner, so
        # it is shown again at its real revision; failing it would bury a live
        # proposal, and reconciliation never revisits a thread that has a row.
        return Step("settle", outcome="resync", reason=UNEXPECTED_REVISION)

    if ledger_status in TERMINAL_STATUSES:
        assert ledger_status is not None
        return Step("settle", outcome=ledger_status.value, final_status=ledger_status.value)

    if view.next:
        if "act" in view.next and decision.action_status is None:
            return Step("settle", outcome="failed", reason=ACT_INTERRUPTED)
        return Step("redrive")

    refused = ledger_status is MessageStatus.FAILED and ledger_error in _REGISTRY_REASONS
    return Step("settle", outcome="failed", reason=ledger_error if refused else NO_OUTCOME)


def apply_open(
    session: GraphSession,
    *,
    announce: Announce | None = None,
    limit: int = 10,
    stop: threading.Event | None = None,
) -> list[tuple[str, str]]:
    """Apply every open decision that is due, one at a time.

    Returns `(message_id, outcome)` pairs for the log. While the owner has
    paused the agent, nothing is applied (M17, D6): a Confirm stays parked,
    so a Withdraw can still reach it.
    """
    applied: list[tuple[str, str]] = []
    if control.is_paused(session.conn):
        return applied
    for decision in _due(session.conn, limit):
        if stop is not None and stop.is_set():
            break
        if not _take_lease(session.conn, decision.id):
            continue  # another worker has it
        try:
            outcome = apply_one(session, decision, announce=announce)
        except Exception:
            # One broken decision must not hold up the others. Its lease
            # expires and the next pass tries it again.
            log.exception(
                "decision %d on %s could not be applied", decision.id, decision.message_id
            )
            outcome = "error"
        applied.append((decision.message_id, outcome))
    return applied


def apply_one(
    session: GraphSession, decision: OpenDecision, *, announce: Announce | None = None
) -> str:
    """Move one leased decision as far as it will go. Returns its outcome, or
    `retrying` when an attempt failed and another is scheduled.

    Any failure costs an attempt, whatever raised it: a model outage, a
    database error in the settle, a step that made no progress. Counting them
    all is what bounds the work a broken decision can cause.
    """
    if decision.attempts >= MAX_ATTEMPTS:
        return _give_up(session, decision, announce)
    try:
        return _advance(session, decision, announce)
    except PausedError:
        # Not a failure: the owner paused the agent (D6) while this decision
        # was being applied. Picked up again as soon as they resume.
        _hold(session.conn, decision)
        return "paused"
    except Exception:
        log.exception("attempt %d on %s failed", decision.attempts + 1, decision.message_id)
        if _record_failure(session.conn, decision) < MAX_ATTEMPTS:
            return "retrying"
        if _with_action_status(session.conn, decision).action_status == "executing":
            # The last write may still land: it is looked up later, not now.
            _wait(session.conn, decision, LOOKUP_DELAY)
            return "retrying"
        return _give_up(session, decision, announce)


def _advance(session: GraphSession, decision: OpenDecision, announce: Announce | None) -> str:
    for _ in range(MAX_PASSES):
        decision = _with_action_status(session.conn, decision)
        view = session.thread(decision.message_id)
        entry = MessageLedger(session.conn).get(decision.message_id)
        ledger_status = entry.status if entry is not None else None
        step = step_for(view, ledger_status, decision, ledger_error=entry.error if entry else None)

        if step.kind == "settle":
            return _settle(
                session.conn, decision, step, view, ledger_status, announce, session.binding()
            )
        if step.kind == "resume":
            session.resume(decision.message_id, _resume_value(decision))
        else:
            session.redrive(decision.message_id)

    raise RuntimeError(f"no progress on {decision.message_id} after {MAX_PASSES} passes")


def _give_up(session: GraphSession, decision: OpenDecision, announce: Announce | None) -> str:
    """No more attempts. Settle from what is stored, as cleanly as it allows.

    A thread still parked at the decision's revision never consumed it: the
    proposal goes back to the owner (`no_effect`) rather than being lost. A
    state that settles on its own terms -- a final ledger, a re-park -- settles
    that way, and so does an action the registry already ran (M17, D3). Only
    a thread stuck mid-graph fails, and never while a write it began may exist.
    """
    conn = session.conn
    try:
        decision = _with_action_status(conn, decision)
        view = session.thread(decision.message_id)
        entry = MessageLedger(conn).get(decision.message_id)
        ledger_status = entry.status if entry is not None else None
        step = step_for(view, ledger_status, decision, ledger_error=entry.error if entry else None)

        if step.kind == "settle":
            return _settle(conn, decision, step, view, ledger_status, announce, session.binding())
        if step.kind == "redrive" and decision.action_status in ("executing", "done"):
            return _unfinished_write(session, decision)
        if step.kind == "redrive" and decision.action_status == "dry_run":
            return _settle_dry_run(conn, decision)

        with conn.transaction():
            if step.kind == "resume":
                if _close(conn, decision.id, "no_effect", reason=ATTEMPTS_EXHAUSTED):
                    conn.execute(
                        """
                        UPDATE proposals SET status = 'pending', updated_at = now()
                         WHERE message_id = %s AND status = 'deciding'
                        """,
                        (decision.message_id,),
                    )
                return "no_effect"
            settle_failed(conn, decision.id, decision.message_id, reason=ATTEMPTS_EXHAUSTED)
            return "failed"
    except Exception:
        # Even the clean settle failed -- a surprise constraint, a conflicting
        # row, a checkpoint that cannot be read. The plainest settle there is
        # ends it, rather than retrying the same failure every pass for ever.
        # Unless a write it began may exist: failing that would be a guess.
        log.exception("could not settle %s cleanly", decision.message_id)
        if _with_action_status(conn, decision).action_status in ("executing", "done"):
            _unconfirmed(conn, decision)
            return "unconfirmed"
        with conn.transaction():
            settle_failed(conn, decision.id, decision.message_id, reason=ATTEMPTS_EXHAUSTED)
        return "failed"


def _resume_value(decision: OpenDecision) -> dict[str, Any]:
    """What a resume hands `await_approval`: the owner's answer and, for a
    Confirm, the approval `act` will be checked against."""
    value: dict[str, Any] = {"action": decision.action, "correction": decision.correction or ""}
    if decision.action_id is not None and decision.nonce is not None:
        value["approval"] = Approval(decision.action_id, decision.nonce).to_state()
    return value


def _with_action_status(conn: psycopg.Connection, decision: OpenDecision) -> OpenDecision:
    """The decision with its action's status as it is now: each attempt moves it."""
    if decision.action_id is None:
        return decision
    row = conn.execute(
        "SELECT status FROM outbound_actions WHERE id = %s", (decision.action_id,)
    ).fetchone()
    return replace(decision, action_status=None if row is None else row[0])


def _unfinished_write(session: GraphSession, decision: OpenDecision) -> str:
    """The attempts ran out with a calendar write begun (M17, D3).

    Google is asked whether the event exists: found, the write happened and
    the decision settles as created; not found, it never will, and the
    decision fails. When Google cannot be asked -- usually why the attempts
    ran out -- nothing is settled on a guess. The decision stays open and
    asks again in an hour.
    """
    conn = session.conn
    registry = session.deps.registry
    assert decision.action_id is not None
    try:
        event_id = registry.look_up(decision.action_id)
    except Exception:
        log.exception("could not confirm the calendar write for %s", decision.message_id)
        _unconfirmed(conn, decision)
        return "unconfirmed"

    with conn.transaction():
        if event_id is None:
            if not settle_failed(conn, decision.id, decision.message_id, reason=ATTEMPTS_EXHAUSTED):
                return "already settled"
            registry.close(decision.action_id, event_id=None)
            return "failed"
        if not _close(conn, decision.id, MessageStatus.CREATED.value, reason=None):
            return "already settled"
        registry.close(decision.action_id, event_id=event_id)
        _mark_final(conn, decision.message_id, MessageStatus.CREATED, event_id=event_id)
        _mark_decided(conn, decision.message_id, MessageStatus.CREATED.value)
        return MessageStatus.CREATED.value


def _settle_dry_run(conn: psycopg.Connection, decision: OpenDecision) -> str:
    """The registry ran the action under `DRY_RUN`, and only the ledger's mark
    was lost: settle as `act` would have."""
    with conn.transaction():
        if not settle_decided(
            conn, decision.id, decision.message_id, final_status=MessageStatus.SKIPPED.value
        ):
            return "already settled"
        _mark_final(conn, decision.message_id, MessageStatus.SKIPPED, error="dry_run")
    return MessageStatus.SKIPPED.value


def _wait(conn: psycopg.Connection, decision: OpenDecision, delay: timedelta) -> None:
    """Release the lease, and come back to the decision after `delay`."""
    conn.execute(
        """
        UPDATE decisions SET next_attempt_at = now() + %s, lease_until = NULL
         WHERE id = %s AND outcome IS NULL
        """,
        (delay, decision.id),
    )


def _unconfirmed(conn: psycopg.Connection, decision: OpenDecision) -> None:
    """Leave the decision open, ask again in an hour, and audit it once."""
    with conn.transaction():
        _wait(conn, decision, UNCONFIRMED_RETRY)
        seen = conn.execute(
            "SELECT 1 FROM audit_log WHERE kind = 'write_unconfirmed' AND decision_id = %s",
            (decision.id,),
        ).fetchone()
        if seen is None:
            audit.record(
                conn,
                "write_unconfirmed",
                decision_id=decision.id,
                message_id=decision.message_id,
                reason=audit.REASONS["unconfirmed"],
            )


def _hold(conn: psycopg.Connection, decision: OpenDecision) -> None:
    """Paused (D6): release the lease without counting an attempt, and stay
    due, so the decision moves on as soon as the owner resumes."""
    conn.execute(
        """
        UPDATE decisions SET lease_until = NULL, next_attempt_at = now()
         WHERE id = %s AND outcome IS NULL
        """,
        (decision.id,),
    )


def _record_failure(conn: psycopg.Connection, decision: OpenDecision) -> int:
    """Count a failed attempt, schedule the next and release the lease.
    Returns the attempts made so far."""
    attempts = decision.attempts + 1
    delay = RETRY_DELAYS[attempts - 1] if attempts <= len(RETRY_DELAYS) else timedelta(0)
    conn.execute(
        """
        UPDATE decisions
           SET attempts = %s, next_attempt_at = now() + %s, lease_until = NULL
         WHERE id = %s AND outcome IS NULL
        """,
        (attempts, delay, decision.id),
    )
    return attempts


def _take_lease(conn: psycopg.Connection, decision_id: int) -> bool:
    row = conn.execute(
        """
        UPDATE decisions SET lease_until = now() + %s
         WHERE id = %s AND outcome IS NULL
           AND (lease_until IS NULL OR lease_until < now())
        RETURNING id
        """,
        (LEASE, decision_id),
    ).fetchone()
    return row is not None


def settle_decided(
    conn: psycopg.Connection, decision_id: int, message_id: str, *, final_status: str
) -> bool:
    """The ledger reached a final status. Must run inside a transaction.

    Returns False, changing nothing, if the decision was already settled.
    """
    require_transaction(conn)
    if not _close(conn, decision_id, final_status, reason=None):
        return False
    _mark_decided(conn, message_id, final_status)
    return True


def settle_failed(
    conn: psycopg.Connection, decision_id: int, message_id: str, *, reason: str
) -> bool:
    """Nothing more can be done for this decision. Must run inside a transaction.

    The ledger becomes FAILED only from `awaiting_approval`: a message that
    reached a final status keeps it, and with it any calendar event id.
    Returns False, changing nothing, if the decision was already settled.
    """
    require_transaction(conn)
    if not _close(conn, decision_id, "failed", reason=reason):
        return False
    _mark_failed(conn, message_id, reason)
    return True


def _settle(
    conn: psycopg.Connection,
    decision: OpenDecision,
    step: Step,
    view: ThreadView,
    ledger_status: MessageStatus | None,
    announce: Announce | None,
    binding: Binding | None = None,
) -> str:
    """Record a step's outcome in one transaction. Returns the outcome.

    Each settle closes its own decision first and changes the rest only if
    that close took effect. A late settle -- from a worker whose lease ran
    out -- therefore never reaches a proposal that has moved on to a newer
    decision.
    """
    shows_again = step.outcome in ("reparked", "resync")
    outcome = "no_effect" if step.outcome == "resync" else step.outcome
    assert outcome is not None
    record = None

    with conn.transaction():
        if shows_again:
            assert view.payload is not None and ledger_status is not None
            if not _close(conn, decision.id, outcome, reason=step.reason):
                return "already settled"
            record = proposal_from(decision.message_id, view.payload, view.revision, binding)
            record = replace(
                record, generation=write_park(conn, record, ledger_status=ledger_status)
            )
        elif outcome == "failed":
            if not settle_failed(
                conn, decision.id, decision.message_id, reason=step.reason or NO_OUTCOME
            ):
                return "already settled"
        else:
            assert step.final_status is not None
            if not settle_decided(
                conn, decision.id, decision.message_id, final_status=step.final_status
            ):
                return "already settled"

    if record is not None and announce is not None:
        _announce(announce, record)
    return outcome


def _mark_decided(conn: psycopg.Connection, message_id: str, final_status: str) -> None:
    conn.execute(
        """
        UPDATE proposals
           SET status = 'decided', final_status = %s, updated_at = now()
         WHERE message_id = %s AND status = 'deciding'
        """,
        (final_status, message_id),
    )


def _mark_final(
    conn: psycopg.Connection,
    message_id: str,
    status: MessageStatus,
    *,
    event_id: str | None = None,
    error: str | None = None,
) -> None:
    """The mark `act` would have written, after the attempts ran out. Only
    from `awaiting_approval`, like every mark the worker makes."""
    conn.execute(
        """
        UPDATE processed_messages
           SET status = %s, calendar_event_id = %s, error = %s, updated_at = now()
         WHERE gmail_message_id = %s AND status = %s
        """,
        (status.value, event_id, error, message_id, MessageStatus.AWAITING_APPROVAL.value),
    )


def _mark_failed(conn: psycopg.Connection, message_id: str, reason: str) -> None:
    conn.execute(
        """
        UPDATE proposals SET status = 'failed', updated_at = now()
         WHERE message_id = %s AND status = 'deciding'
        """,
        (message_id,),
    )
    conn.execute(
        """
        UPDATE processed_messages SET status = %s, error = %s, updated_at = now()
         WHERE gmail_message_id = %s AND status = %s
        """,
        (
            MessageStatus.FAILED.value,
            reason,
            message_id,
            MessageStatus.AWAITING_APPROVAL.value,
        ),
    )


def _announce(announce: Announce, record: ProposalRecord) -> None:
    try:
        announce(record)
    except Exception:
        # The re-park is recorded, and the timeline shows it.
        log.exception("could not announce %s", record.message_id)


def _close(conn: psycopg.Connection, decision_id: int, outcome: str, *, reason: str | None) -> bool:
    """Write the outcome, once. True if this call wrote it."""
    closed = conn.execute(
        """
        UPDATE decisions
           SET outcome = %s, reason = %s, settled_at = now(), lease_until = NULL
         WHERE id = %s AND outcome IS NULL
        """,
        (outcome, reason, decision_id),
    ).rowcount
    return bool(closed)


def _due(conn: psycopg.Connection, limit: int) -> list[OpenDecision]:
    rows = conn.execute(
        """
        SELECT d.id, d.message_id, d.revision, d.action, d.correction, d.attempts,
               a.id, a.nonce, a.status
          FROM decisions d
          LEFT JOIN outbound_actions a ON a.decision_id = d.id
         WHERE d.outcome IS NULL AND d.next_attempt_at <= now()
           AND (d.lease_until IS NULL OR d.lease_until < now())
         ORDER BY d.decided_at, d.id
         LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [OpenDecision(*row) for row in rows]
