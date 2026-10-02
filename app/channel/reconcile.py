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

**Every pending proposal can be bound, and runs as it was made (M17, D2).**
One parked before M17 has no hash: it gets its tool and hash from its
thread's payload under the current code, while it is still pending at the
revision read, and can then be confirmed. One made under the other
`DRY_RUN`, awaiting the owner, is expired: a sweep ends it, "made under
another mode". Both passes run only when asked, and only the scheduler asks:
it runs under production's `DRY_RUN`, calendar and key. A command-line run
under its own would bind with the wrong hash, or expire live proposals. For
the same reason, a command-line run records no missing row: it counts them,
and the scheduler's next pass records, binds and announces each.

A `deciding` proposal is never touched: it belongs to the worker.

Runs when the scheduler starts, hourly after that, and on
`approve --reconcile`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import psycopg

from app.channel.decide import expire
from app.channel.park import Announce, ParkConflictError, record_park
from app.graph.runner import GraphSession
from app.jobs.purge import FAILED_KEPT_FOR
from app.policy.hashing import bound
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

    bound: int = 0
    """Pending proposals from before M17 given their tool and hash."""

    expired: int = 0
    """Pending proposals made under the other `DRY_RUN`, swept."""

    left: int = 0
    """Parked threads with no row, left for the scheduler's pass: a
    command-line run records none."""


def reconcile(
    session: GraphSession, *, announce: Announce | None = None, bind_and_expire: bool = False
) -> ReconcileResult:
    recorded = closed = errors = 0

    left = 0
    for message_id in _unrecorded(session.conn):
        try:
            view = session.thread(message_id)
            if not view.parked:
                continue
            assert view.payload is not None
            if not bind_and_expire:
                # A command-line run records nothing: under its own settings,
                # a hash could be one production refuses, and a card from the
                # other mode could be pushed as live. The scheduler's next pass
                # records, binds and announces it.
                left += 1
                continue
            # One about to be expired below is recorded but never announced.
            doomed = bool(view.payload.get("dry_run", True)) != session.dry_run
            record_park(session, message_id, view.payload, announce=None if doomed else announce)
            recorded += 1
        except ParkConflictError:
            # The ledger moved between the read and the write: someone else
            # got there first. Nothing to repair.
            log.info("%s changed while being reconciled", message_id)
        except Exception:
            log.exception("could not reconcile %s", message_id)
            errors += 1

    bound_rows = expired = 0
    for message_id, revision in _unbound(session.conn) if bind_and_expire else []:
        try:
            view = session.thread(message_id)
            if not view.parked or view.revision != revision:
                continue
            assert view.payload is not None
            tool, digest = bound(message_id, view.payload.get("proposed") or {}, session.binding())
            if digest is None:
                continue  # nothing to run: a Confirm stays "not ready"
            bound_rows += session.conn.execute(
                """
                UPDATE proposals SET tool = %s, args_hash = %s, updated_at = now()
                 WHERE message_id = %s AND status = 'pending' AND revision = %s
                   AND args_hash IS NULL
                """,
                (tool, digest, message_id, revision),
            ).rowcount
        except Exception:
            log.exception("could not bind %s", message_id)
            errors += 1

    for message_id, final_status in _closable(session.conn):
        try:
            # A final ledger beside a live interrupt is contradictory. Leave
            # it for a person rather than guess which half is wrong -- but
            # counted, so the tick is not reported healthy.
            if session.thread(message_id).parked:
                log.warning(
                    "%s is %s yet still parked; left for a person", message_id, final_status
                )
                errors += 1
                continue
            if _close(session.conn, message_id, final_status):
                closed += 1
        except Exception:
            log.exception("could not close %s", message_id)
            errors += 1

    # After the closing pass: a row whose message is already final is closed,
    # not expired.
    for message_id, revision in (
        _made_under_another_mode(session.conn, session.dry_run) if bind_and_expire else []
    ):
        try:
            expired += expire(session.conn, message_id, revision)
        except Exception:
            log.exception("could not expire %s", message_id)
            errors += 1

    if recorded or closed or bound_rows or expired:
        log.info(
            "reconcile: recorded %d parked thread(s), closed %d row(s), bound %d, expired %d",
            recorded,
            closed,
            bound_rows,
            expired,
        )
    return ReconcileResult(
        recorded=recorded,
        closed=closed,
        errors=errors,
        bound=bound_rows,
        expired=expired,
        left=left,
    )


def _unbound(conn: psycopg.Connection) -> list[tuple[str, int]]:
    """Pending proposals with no hash yet: parked before M17."""
    rows = conn.execute(
        "SELECT message_id, revision FROM proposals WHERE status = 'pending' AND args_hash IS NULL"
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _made_under_another_mode(conn: psycopg.Connection, dry_run: bool) -> list[tuple[str, int]]:
    """Pending proposals made under the other `DRY_RUN`, still awaiting the
    owner."""
    rows = conn.execute(
        """
        SELECT p.message_id, p.revision
          FROM proposals p
          JOIN processed_messages m ON m.gmail_message_id = p.message_id
         WHERE p.status = 'pending' AND p.dry_run <> %s AND m.status = %s
        """,
        (dry_run, MessageStatus.AWAITING_APPROVAL.value),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


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
