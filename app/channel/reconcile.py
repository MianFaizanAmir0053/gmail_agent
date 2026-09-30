"""Reconciliation (M16, D3).

Two promises, repaired rather than assumed:

**A live interrupt always has a row.** A thread parked at `await_approval` is
waiting for the owner, and the owner sees only what has a `proposals` row. The
checkpoint that parks a thread and the park step that writes its rows run on
different connections, so a crash between them -- or a run that parked and
then raised in the tracer's bookkeeping, which poll dead-letters -- leaves a
parked thread nobody can see. Proposals parked before M16 have no row either.

**Rows follow the ledger.** A `pending` or `failed` proposal whose message
reached a final status, with its thread no longer parked, is closed as
`decided`. The M15 CLI, which resumes threads directly until 16.10, leaves
exactly that behind.

A `deciding` proposal is never touched: it belongs to the worker.

Runs when the scheduler starts, hourly after that, and on
`approve --reconcile`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import psycopg

from app.channel.park import Announce, ParkConflictError, record_park
from app.graph.runner import GraphSession
from app.jobs.purge import FAILED_KEPT_FOR
from app.store.ledger import MessageStatus

log = logging.getLogger(__name__)

CLAIM_GRACE = timedelta(minutes=10)
"""How old a `claimed` row must be before reconciliation reads its thread.

Poll claims a message, runs it, and writes its rows within seconds. A younger
claim may be one poll is parking right now, and recording it here would race
that poll for the same rows."""


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    recorded: int
    """Parked threads given the row they were missing."""

    closed: int
    """Rows closed as `decided` because their ledger is final."""

    errors: int
    """Threads that could not be read or recorded. Tried again next run."""


def reconcile(session: GraphSession, *, announce: Announce | None = None) -> ReconcileResult:
    recorded = closed = errors = 0

    for message_id in _unrecorded(session.conn):
        try:
            view = session.thread(message_id)
            if not view.parked:
                continue
            assert view.payload is not None
            record_park(session, message_id, view.payload, announce=announce)
            recorded += 1
        except ParkConflictError:
            # The ledger moved between the read and the write: someone else
            # got there first. Nothing to repair.
            log.info("%s changed while being reconciled", message_id)
        except Exception:
            log.exception("could not reconcile %s", message_id)
            errors += 1

    for message_id, final_status in _closable(session.conn):
        try:
            # A final ledger beside a live interrupt is contradictory. Leave
            # it for a person rather than guess which half is wrong.
            if session.thread(message_id).parked:
                continue
            if _close(session.conn, message_id, final_status):
                closed += 1
        except Exception:
            log.exception("could not close %s", message_id)
            errors += 1

    if recorded or closed:
        log.info("reconcile: recorded %d parked thread(s), closed %d row(s)", recorded, closed)
    return ReconcileResult(recorded=recorded, closed=closed, errors=errors)


def _unrecorded(conn: psycopg.Connection) -> list[str]:
    """Ledger rows whose thread may be parked and that have no proposal row.

    FAILED rows are read only while the purge keeps their checkpoint.
    """
    rows = conn.execute(
        """
        SELECT m.gmail_message_id
          FROM processed_messages m
          LEFT JOIN proposals p ON p.message_id = m.gmail_message_id
         WHERE p.message_id IS NULL
           AND (   (m.status = %s AND m.updated_at < now() - %s)
                OR  m.status = %s
                OR (m.status = %s AND m.updated_at >= now() - %s))
         ORDER BY m.updated_at
        """,
        (
            MessageStatus.CLAIMED.value,
            CLAIM_GRACE,
            MessageStatus.AWAITING_APPROVAL.value,
            MessageStatus.FAILED.value,
            FAILED_KEPT_FOR,
        ),
    ).fetchall()
    return [row[0] for row in rows]


def _closable(conn: psycopg.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT p.message_id, m.status
          FROM proposals p
          JOIN processed_messages m ON m.gmail_message_id = p.message_id
         WHERE p.status IN ('pending', 'failed')
           AND m.status IN (%s, %s, %s)
        """,
        (
            MessageStatus.CREATED.value,
            MessageStatus.SKIPPED.value,
            MessageStatus.REJECTED.value,
        ),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _close(conn: psycopg.Connection, message_id: str, final_status: str) -> bool:
    return bool(
        conn.execute(
            """
            UPDATE proposals
               SET status = 'decided', final_status = %s, updated_at = now()
             WHERE message_id = %s AND status IN ('pending', 'failed')
            """,
            (final_status, message_id),
        ).rowcount
    )
