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
| Stopped before `act`                           | settle: failed, never re-run  |
| Anything else                                  | settle: failed                |

A settle is one transaction, and each write in it is conditional on the
state it expects, so settling twice changes nothing.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Literal

import psycopg

from app.channel.park import Announce, proposal_from, write_park
from app.graph.runner import GraphSession, ThreadView
from app.store.ledger import TERMINAL_STATUSES, MessageLedger, MessageStatus

log = logging.getLogger(__name__)

ACT_INTERRUPTED = "act interrupted"
"""The thread stopped with `act` next. Creating an event is not idempotent, so
it is never run again: under DRY_RUN nothing was booked, and M17's
deterministic event id is what will make a re-run safe."""

UNEXPECTED_REVISION = "unexpected revision"
NO_OUTCOME = "no outcome recorded"

MAX_PASSES = 3
"""Resume or re-drive, then settle, takes two passes. A third means a step
made no progress, which is reported rather than retried in a loop."""


@dataclass(frozen=True, slots=True)
class OpenDecision:
    id: int
    message_id: str
    revision: int
    action: str
    correction: str | None
    attempts: int


@dataclass(frozen=True, slots=True)
class Step:
    kind: Literal["resume", "redrive", "settle"]
    outcome: str | None = None
    final_status: str | None = None
    reason: str | None = None


def step_for(view: ThreadView, ledger_status: MessageStatus | None, decision: OpenDecision) -> Step:
    """The next step for an open decision, from stored state alone."""
    if view.parked:
        if view.revision == decision.revision:
            return Step("resume")
        if decision.action == "edit" and view.revision == decision.revision + 1:
            return Step("settle", outcome="reparked")
        return Step("settle", outcome="failed", reason=UNEXPECTED_REVISION)

    if ledger_status in TERMINAL_STATUSES:
        assert ledger_status is not None
        return Step("settle", outcome=ledger_status.value, final_status=ledger_status.value)

    if view.next:
        if "act" in view.next:
            return Step("settle", outcome="failed", reason=ACT_INTERRUPTED)
        return Step("redrive")

    return Step("settle", outcome="failed", reason=NO_OUTCOME)


def apply_open(
    session: GraphSession,
    *,
    announce: Announce | None = None,
    limit: int = 10,
    stop: threading.Event | None = None,
) -> list[tuple[str, str]]:
    """Apply every open decision that is due, one at a time.

    Returns `(message_id, outcome)` pairs for the log.
    """
    applied: list[tuple[str, str]] = []
    for decision in _due(session.conn, limit):
        if stop is not None and stop.is_set():
            break
        applied.append((decision.message_id, apply_one(session, decision, announce=announce)))
    return applied


def apply_one(
    session: GraphSession, decision: OpenDecision, *, announce: Announce | None = None
) -> str:
    """Move one decision until it settles. Returns its outcome."""
    for _ in range(MAX_PASSES):
        view = session.thread(decision.message_id)
        entry = MessageLedger(session.conn).get(decision.message_id)
        ledger_status = entry.status if entry is not None else None
        step = step_for(view, ledger_status, decision)

        if step.kind == "settle":
            _settle(session.conn, decision, step, view, ledger_status, announce)
            assert step.outcome is not None
            return step.outcome
        if step.kind == "resume":
            session.resume(
                decision.message_id,
                {"action": decision.action, "correction": decision.correction or ""},
            )
        else:
            session.redrive(decision.message_id)

    raise RuntimeError(f"no progress on {decision.message_id} after {MAX_PASSES} passes")


def settle_decided(
    conn: psycopg.Connection, decision_id: int, message_id: str, *, final_status: str
) -> None:
    """The ledger reached a final status. Must run inside a transaction."""
    conn.execute(
        """
        UPDATE proposals
           SET status = 'decided', final_status = %s, updated_at = now()
         WHERE message_id = %s AND status = 'deciding'
        """,
        (final_status, message_id),
    )
    _close(conn, decision_id, final_status, reason=None)


def settle_failed(
    conn: psycopg.Connection, decision_id: int, message_id: str, *, reason: str
) -> None:
    """Nothing more can be done for this decision. Must run inside a transaction.

    The ledger becomes FAILED only from `awaiting_approval`: a message that
    reached a final status keeps it, and with it any calendar event id.
    """
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
    _close(conn, decision_id, "failed", reason=reason)


def _settle(
    conn: psycopg.Connection,
    decision: OpenDecision,
    step: Step,
    view: ThreadView,
    ledger_status: MessageStatus | None,
    announce: Announce | None,
) -> None:
    with conn.transaction():
        if step.outcome == "reparked":
            assert view.payload is not None and ledger_status is not None
            write_park(
                conn,
                proposal_from(decision.message_id, view.payload, view.revision),
                ledger_status=ledger_status,
            )
            _close(conn, decision.id, "reparked", reason=None)
        elif step.outcome == "failed":
            settle_failed(conn, decision.id, decision.message_id, reason=step.reason or NO_OUTCOME)
        else:
            assert step.final_status is not None
            settle_decided(conn, decision.id, decision.message_id, final_status=step.final_status)

    if step.outcome == "reparked" and announce is not None:
        assert view.payload is not None
        _announce(announce, decision.message_id, view.payload)


def _announce(announce: Announce, message_id: str, payload: dict[str, Any]) -> None:
    try:
        announce(message_id, payload)
    except Exception:
        # The re-park is recorded, and the timeline shows it.
        log.exception("could not announce %s", message_id)


def _close(conn: psycopg.Connection, decision_id: int, outcome: str, *, reason: str | None) -> None:
    conn.execute(
        """
        UPDATE decisions
           SET outcome = %s, reason = %s, settled_at = now(), lease_until = NULL
         WHERE id = %s AND outcome IS NULL
        """,
        (outcome, reason, decision_id),
    )


def _due(conn: psycopg.Connection, limit: int) -> list[OpenDecision]:
    rows = conn.execute(
        """
        SELECT id, message_id, revision, action, correction, attempts
          FROM decisions
         WHERE outcome IS NULL AND next_attempt_at <= now()
         ORDER BY decided_at, id
         LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [OpenDecision(*row) for row in rows]
