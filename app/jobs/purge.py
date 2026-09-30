"""Checkpoint retention (M15).

Every polled message's full body is stored in its graph checkpoint, including
unread mail carrying one-time codes and password-reset links, and nothing used
to delete it. The purge keeps what a pending decision still needs and removes
the rest:

- a thread whose outcome is final loses its checkpoint;
- a FAILED thread keeps its checkpoint for a week, for diagnosis;
- a thread awaiting approval, or still being processed, is never touched.

It also clears model-written text from the ledger after a week. `skip` stores
the extractor's reasoning, which quotes the email it read. The fixed phrases
that operators and later statistics depend on stay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import psycopg

from app.graph.checkpointer import postgres_checkpointer
from app.graph.nodes import SWEEP_REASON
from app.jobs.poll import CLAIMED_NOT_RUN
from app.store.ledger import STRANDED_REASON, MessageStatus

FINAL_STATUSES = (MessageStatus.SKIPPED, MessageStatus.REJECTED, MessageStatus.CREATED)

FAILED_KEPT_FOR = timedelta(days=7)
REASONS_KEPT_FOR = timedelta(days=7)

FIXED_REASONS = (
    "declined by user",
    "rejected by reviewer",
    SWEEP_REASON,
    STRANDED_REASON,
    CLAIMED_NOT_RUN,
)
"""Ledger reasons written by code rather than by a model. They quote no email,
and they are what the failures view and M24's statistics are built from."""


@dataclass(frozen=True, slots=True)
class PurgeResult:
    threads: int
    """Threads whose checkpoints were deleted."""

    reasons_cleared: int
    """Ledger rows whose model-written text was removed or cut to its type."""


def purge(conn: psycopg.Connection, database_url: str) -> PurgeResult:
    # Opened first: its setup() creates the checkpoint tables on a fresh
    # database, so the query below never fails for want of them.
    with postgres_checkpointer(database_url) as saver:
        thread_ids = [
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT c.thread_id
                  FROM checkpoints c
                  JOIN processed_messages p ON p.gmail_message_id = c.thread_id
                 WHERE p.status = ANY(%s)
                    OR (p.status = %s AND p.updated_at < now() - %s)
                """,
                (
                    [status.value for status in FINAL_STATUSES],
                    MessageStatus.FAILED.value,
                    FAILED_KEPT_FOR,
                ),
            ).fetchall()
        ]
        for thread_id in thread_ids:
            saver.delete_thread(thread_id)

    return PurgeResult(threads=len(thread_ids), reasons_cleared=_clear_reasons(conn))


def _clear_reasons(conn: psycopg.Connection) -> int:
    """Remove model-written text from ledger rows older than a week.

    `updated_at` is left alone, so clearing a reason does not reset the age
    the other retention rules read.
    """
    cleared = conn.execute(
        """
        UPDATE processed_messages SET error = NULL
         WHERE status = ANY(%s) AND error IS NOT NULL AND NOT (error = ANY(%s))
           AND updated_at < now() - %s
        """,
        (
            [MessageStatus.SKIPPED.value, MessageStatus.REJECTED.value],
            list(FIXED_REASONS),
            REASONS_KEPT_FOR,
        ),
    ).rowcount
    # A failure keeps its exception type, which is what diagnosis needs after
    # a week, and loses the message, which can carry content.
    cleared += conn.execute(
        """
        UPDATE processed_messages SET error = split_part(error, ':', 1)
         WHERE status = %s AND error LIKE '%%:%%' AND NOT (error = ANY(%s))
           AND updated_at < now() - %s
        """,
        (MessageStatus.FAILED.value, list(FIXED_REASONS), REASONS_KEPT_FOR),
    ).rowcount
    return cleared
