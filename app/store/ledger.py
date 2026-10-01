"""The idempotency ledger.

A scheduled poller with no ledger re-processes every message on every restart
and on every overlapping run. That is not something an LLM can fix -- it is a
uniqueness constraint.

`claim()` is the load-bearing method: `INSERT ... ON CONFLICT DO NOTHING`
atomically decides who owns a message. Two pollers racing on the same inbox both
call it; exactly one gets True. Checking "is it processed?" and then inserting
would be a textbook race -- both would read "no" before either wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import psycopg


class MessageStatus(StrEnum):
    CLAIMED = "claimed"
    """Owned by a run, outcome not yet known."""

    EXTRACTED = "extracted"
    AWAITING_APPROVAL = "awaiting_approval"
    CREATED = "created"
    SKIPPED = "skipped"
    """Correctly examined and deliberately not booked -- a newsletter."""

    REJECTED = "rejected"
    """A human said no."""

    FAILED = "failed"


TERMINAL_STATUSES = frozenset(
    {MessageStatus.CREATED, MessageStatus.SKIPPED, MessageStatus.REJECTED}
)
"""Statuses that mean "do not look at this message again".

FAILED is deliberately excluded: a failure is usually transient (rate limit,
timeout) and should be retryable without hand-editing rows.
"""


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    gmail_message_id: str
    thread_id: str
    status: MessageStatus
    calendar_event_id: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


STRANDED_REASON = "stranded by shutdown"
"""A claim found at boot that the feed would not offer again (M20, D4)."""


class StatusTransitionError(ValueError):
    """Attempted a status change the schema or the workflow forbids."""


class MessageLedger:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def claim(self, gmail_message_id: str, thread_id: str) -> bool:
        """Take ownership of a message. True if this caller won.

        False means someone already claimed it -- a concurrent run, or a
        previous run of this one. Either way, skip it.
        """
        row = self._conn.execute(
            """
            INSERT INTO processed_messages (gmail_message_id, thread_id, status)
            VALUES (%s, %s, %s)
            ON CONFLICT (gmail_message_id) DO NOTHING
            RETURNING gmail_message_id
            """,
            (gmail_message_id, thread_id, MessageStatus.CLAIMED.value),
        ).fetchone()
        return row is not None

    def release(self, gmail_message_id: str) -> bool:
        """Give up a claim, so the message is offered again. True if released.

        A claim is taken before its graph runs and is replaced by the graph's
        own outcome. If the process dies in between, the row stays `claimed`
        for ever: nothing offers the message again. At boot the feed decides
        which such claims to release (`app/mail/feed.py`); only a claim is
        ever released, never an outcome. The caller deletes the thread's
        checkpoint first.
        """
        return bool(
            self._conn.execute(
                "DELETE FROM processed_messages WHERE gmail_message_id = %s AND status = %s",
                (gmail_message_id, MessageStatus.CLAIMED.value),
            ).rowcount
        )

    def mark(
        self,
        gmail_message_id: str,
        status: MessageStatus,
        *,
        calendar_event_id: str | None = None,
        error: str | None = None,
    ) -> None:
        if status is MessageStatus.CREATED and not calendar_event_id:
            raise StatusTransitionError("CREATED requires a calendar_event_id")
        if status is not MessageStatus.CREATED and calendar_event_id:
            raise StatusTransitionError(f"{status} must not carry a calendar_event_id")

        updated = self._conn.execute(
            """
            UPDATE processed_messages
               SET status = %s, calendar_event_id = %s, error = %s, updated_at = now()
             WHERE gmail_message_id = %s
            """,
            (status.value, calendar_event_id, error, gmail_message_id),
        ).rowcount
        if not updated:
            raise StatusTransitionError(f"No ledger row for {gmail_message_id!r}; claim it first")

    def get(self, gmail_message_id: str) -> LedgerEntry | None:
        row = self._conn.execute(
            """
            SELECT gmail_message_id, thread_id, status, calendar_event_id,
                   error, created_at, updated_at
              FROM processed_messages
             WHERE gmail_message_id = %s
            """,
            (gmail_message_id,),
        ).fetchone()
        if row is None:
            return None
        return LedgerEntry(
            gmail_message_id=row[0],
            thread_id=row[1],
            status=MessageStatus(row[2]),
            calendar_event_id=row[3],
            error=row[4],
            created_at=row[5],
            updated_at=row[6],
        )

    def unseen(self, gmail_message_ids: list[str]) -> list[str]:
        """Filter a batch down to messages with no ledger row.

        Cheap pre-filter before the expensive path; `claim()` remains the
        authority, since another run can insert between this call and that one.
        """
        if not gmail_message_ids:
            return []
        rows = self._conn.execute(
            "SELECT gmail_message_id FROM processed_messages WHERE gmail_message_id = ANY(%s)",
            (gmail_message_ids,),
        ).fetchall()
        known = {row[0] for row in rows}
        return [mid for mid in gmail_message_ids if mid not in known]

    def counts_by_status(self) -> dict[MessageStatus, int]:
        rows = self._conn.execute(
            "SELECT status, count(*) FROM processed_messages GROUP BY status"
        ).fetchall()
        return {MessageStatus(row[0]): row[1] for row in rows}


class SyncCursor:
    """The Gmail `historyId` watermark. Exactly one row, enforced by the schema."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get(self) -> str | None:
        row = self._conn.execute("SELECT last_history_id FROM sync_state WHERE id = 1").fetchone()
        return row[0] if row else None

    def set(self, history_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO sync_state (id, last_history_id, updated_at)
            VALUES (1, %s, now())
            ON CONFLICT (id) DO UPDATE
               SET last_history_id = EXCLUDED.last_history_id,
                   updated_at = now()
            """,
            (history_id,),
        )
