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
from dataclasses import dataclass, replace
from typing import Any

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from app.graph.runner import GraphSession
from app.graph.versioning import LEGACY_PIPELINE_VERSION, ActionType, action_type
from app.policy.hashing import Binding, bound, shown
from app.store.ledger import TERMINAL_STATUSES, MessageLedger, MessageStatus

log = logging.getLogger(__name__)

Announce = Callable[["ProposalRecord"], None]
"""Tells the owner a proposal needs them.

It gets the stored record -- the card's fields, the revision, the mode --
never the raw interrupt payload, which carries the model's reasoning and can
quote the email."""


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
    tool: str | None = None
    """What a Confirm would run (M17, D1); None when nothing can run."""
    args_hash: str | None = None
    """The keyed hash of exactly what it would send (M17, D2), taken from the
    payload under the current code at every write."""
    generation: int = 1
    """The row's generation, which goes up whenever the proposal returns to
    the owner (M17, D2). Read back from the row at every write, so a card
    announced from this record carries the token the row will accept."""


def proposal_from(
    message_id: str,
    pending: dict[str, Any],
    revision: int,
    binding: Binding | None = None,
) -> ProposalRecord:
    """The row for an interrupt payload.

    A payload parked before M16 lacks the action type and pipeline version,
    and one parked before M15 lacks `dry_run`. The revision is never taken
    from the payload: callers read it from the thread's state.

    With a `binding` (every caller in production passes the session's), the
    row also gets the tool and the keyed hash a Confirm will be bound to, taken
    under the current code. Without one -- tests that never confirm -- both
    stay None, and a Confirm is refused as not ready.
    """
    proposed: dict[str, Any] = pending.get("proposed") or {}
    attendees = list(proposed.get("attendees") or [])
    tool, args_hash = bound(message_id, proposed, binding) if binding else (None, None)
    return ProposalRecord(
        message_id=message_id,
        revision=revision,
        action_type=pending.get("action_type") or action_type(attendees),
        pipeline_version=pending.get("pipeline_version") or LEGACY_PIPELINE_VERSION,
        # Unrecorded means unknown, and unknown is treated as a dry run: M17
        # must never act for real on a proposal whose mode nobody wrote down.
        dry_run=bool(pending.get("dry_run", True)),
        # Not the model's reasoning or confidence: the reasoning can quote the
        # email, and the card does not need either. The title and the location
        # are scrubbed as the event's arguments are (M18, D6), so the card
        # shows what the hash binds, for a payload parked before that too.
        payload={
            "title": shown(proposed.get("title")),
            "start_utc": proposed.get("start_utc"),
            "end_utc": proposed.get("end_utc"),
            "timezone": proposed.get("timezone"),
            "attendees": attendees,
            "location": shown(proposed.get("location")),
            "conflicts": list(pending.get("conflicts") or []),
            "review_issues": list(pending.get("review_issues") or []),
            "outside_guests": list(pending.get("outside_guests") or []),
        },
        tool=tool,
        args_hash=args_hash,
    )


def write_park(
    conn: psycopg.Connection, record: ProposalRecord, *, ledger_status: MessageStatus
) -> int:
    """Both writes. Must run inside the caller's transaction. Returns the
    row's generation, for the record a card is drawn from.

    `ledger_status` is what the caller read. The ledger moves only from that
    status, so a message that reached a final status in the meantime is left
    alone, and the proposal row is written only when it is new or mid-decision.
    Anything else raises `ParkConflictError`, which rolls the transaction back.
    """
    require_transaction(conn)
    if ledger_status in TERMINAL_STATUSES:
        raise ParkConflictError(f"{record.message_id} is already {ledger_status}")
    _move_ledger(conn, record.message_id, ledger_status)
    return _upsert_proposal(conn, record)


def require_transaction(conn: psycopg.Connection) -> None:
    """Refuse to run a multi-statement write outside a transaction.

    On an autocommit connection each statement would commit on its own, and a
    failure between two of them would leave half the change behind.
    """
    if conn.info.transaction_status is not TransactionStatus.INTRANS:
        raise RuntimeError("this write must run inside a transaction")


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

    record = proposal_from(message_id, pending, session.revision(message_id), session.binding())
    with session.conn.transaction():
        generation = write_park(session.conn, record, ledger_status=entry.status)
    record = replace(record, generation=generation)

    if announce is not None:
        try:
            announce(record)
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


def _upsert_proposal(conn: psycopg.Connection, record: ProposalRecord) -> int:
    row = conn.execute(
        """
        INSERT INTO proposals
               (message_id, revision, status, action_type, pipeline_version,
                payload, dry_run, tool, args_hash, parked_at, updated_at)
        VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (message_id) DO UPDATE
           SET revision = EXCLUDED.revision,
               status = 'pending',
               final_status = NULL,
               action_type = EXCLUDED.action_type,
               pipeline_version = EXCLUDED.pipeline_version,
               payload = EXCLUDED.payload,
               dry_run = EXCLUDED.dry_run,
               tool = EXCLUDED.tool,
               args_hash = EXCLUDED.args_hash,
               -- Back to the owner: a Confirm from any card shown before
               -- carries the old generation, and dies (M17, D2).
               generation = proposals.generation + 1,
               parked_at = now(),
               updated_at = now()
         WHERE proposals.status = 'deciding'
        RETURNING generation
        """,
        (
            record.message_id,
            record.revision,
            record.action_type,
            record.pipeline_version,
            Jsonb(record.payload),
            record.dry_run,
            record.tool,
            record.args_hash,
        ),
    ).fetchone()
    if row is None:
        raise ParkConflictError(f"{record.message_id} already has a proposal that is not deciding")
    return int(row[0])
