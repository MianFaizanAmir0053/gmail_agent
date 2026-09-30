"""The park step (M16, D2).

A parked thread becomes visible to the owner through two rows: the ledger's
`awaiting_approval` and a `proposals` row the web app reads. They are written
in one transaction, so no reader ever sees one without the other. The
checkpoint that parked the thread lives on another connection and cannot join
that transaction; a crash between the two is what reconciliation (D3) repairs.

Poll calls this after a run parks, the worker after an edit re-parks, and
reconciliation for a parked thread that has no row.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.graph.runner import GraphSession
from app.graph.versioning import LEGACY_PIPELINE_VERSION, ActionType, action_type
from app.store.ledger import TERMINAL_STATUSES, MessageLedger, MessageStatus

log = logging.getLogger(__name__)

Announce = Callable[[str, dict[str, Any]], None]
"""Tells the owner a proposal needs them: `(message_id, interrupt payload)`."""


class ParkConflictError(RuntimeError):
    """The ledger or the proposal was not in the state the park expected.
    Raised inside the transaction, so neither row is written."""


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    message_id: str
    revision: int
    action_type: ActionType
    pipeline_version: str
    dry_run: bool
    payload: dict[str, Any]
    """What the card shows (D2). Cleared by retention (D8)."""


def proposal_from(message_id: str, pending: dict[str, Any], revision: int) -> ProposalRecord:
    """The row for an interrupt payload.

    A payload parked before M16 lacks the action type and pipeline version,
    and one parked before M15 lacks `dry_run`. The revision is never taken
    from the payload: callers read it from the thread's state.
    """
    proposed: dict[str, Any] = pending.get("proposed") or {}
    attendees = list(proposed.get("attendees") or [])
    return ProposalRecord(
        message_id=message_id,
        revision=revision,
        action_type=pending.get("action_type") or action_type(attendees),
        pipeline_version=pending.get("pipeline_version") or LEGACY_PIPELINE_VERSION,
        # Unrecorded means unknown, and unknown is treated as a dry run: M17
        # must never act for real on a proposal whose mode nobody wrote down.
        dry_run=bool(pending.get("dry_run", True)),
        # Not the model's reasoning or confidence: the reasoning can quote the
        # email, and the card does not need either.
        payload={
            "title": proposed.get("title"),
            "start_utc": proposed.get("start_utc"),
            "end_utc": proposed.get("end_utc"),
            "timezone": proposed.get("timezone"),
            "attendees": attendees,
            "location": proposed.get("location"),
            "conflicts": list(pending.get("conflicts") or []),
            "review_issues": list(pending.get("review_issues") or []),
        },
    )


def write_park(
    conn: psycopg.Connection, record: ProposalRecord, *, ledger_status: MessageStatus
) -> None:
    """Both writes. Must run inside the caller's transaction.

    `ledger_status` is what the caller read. The ledger moves only from that
    status, so a message that reached a final status in the meantime is left
    alone, and the proposal row is written only when it is new or mid-decision.
    Anything else raises `ParkConflictError`, which rolls the transaction back.
    """
    if ledger_status in TERMINAL_STATUSES:
        raise ParkConflictError(f"{record.message_id} is already {ledger_status}")
    _move_ledger(conn, record.message_id, ledger_status)
    _upsert_proposal(conn, record)


def record_park(
    session: GraphSession,
    message_id: str,
    pending: dict[str, Any],
    *,
    announce: Announce | None = None,
) -> ProposalRecord:
    """Record a parked thread, then announce it."""
    entry = MessageLedger(session.conn).get(message_id)
    if entry is None:
        raise ParkConflictError(f"no ledger row for {message_id}")

    record = proposal_from(message_id, pending, session.revision(message_id))
    with session.conn.transaction():
        write_park(session.conn, record, ledger_status=entry.status)

    if announce is not None:
        try:
            announce(message_id, pending)
        except Exception:
            # The proposal is durably recorded and the timeline shows it; a
            # channel being down must not undo that.
            log.exception("could not announce %s", message_id)
    return record


def _move_ledger(conn: psycopg.Connection, message_id: str, expected: MessageStatus) -> None:
    moved = conn.execute(
        """
        UPDATE processed_messages
           SET status = %s, error = NULL, updated_at = now()
         WHERE gmail_message_id = %s AND status = %s
        """,
        (MessageStatus.AWAITING_APPROVAL.value, message_id, expected.value),
    ).rowcount
    if not moved:
        raise ParkConflictError(f"{message_id} is no longer {expected}")


def _upsert_proposal(conn: psycopg.Connection, record: ProposalRecord) -> None:
    row = conn.execute(
        """
        INSERT INTO proposals
               (message_id, revision, status, action_type, pipeline_version,
                payload, dry_run, parked_at, updated_at)
        VALUES (%s, %s, 'pending', %s, %s, %s, %s, now(), now())
        ON CONFLICT (message_id) DO UPDATE
           SET revision = EXCLUDED.revision,
               status = 'pending',
               final_status = NULL,
               action_type = EXCLUDED.action_type,
               pipeline_version = EXCLUDED.pipeline_version,
               payload = EXCLUDED.payload,
               dry_run = EXCLUDED.dry_run,
               parked_at = now(),
               updated_at = now()
         WHERE proposals.status = 'deciding'
        RETURNING message_id
        """,
        (
            record.message_id,
            record.revision,
            record.action_type,
            record.pipeline_version,
            Jsonb(record.payload),
            record.dry_run,
        ),
    ).fetchone()
    if row is None:
        raise ParkConflictError(f"{record.message_id} already has a proposal that is not deciding")
