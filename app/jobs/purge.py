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

The web channel's records follow the same clock (M16, D8). A proposal's card
and the owner's corrections quote the mail too, so they are cleared a week
after the ledger row settled, and expired pairing codes are deleted. What M24
counts autonomy from -- action type, pipeline version, channel, revision,
outcome and timings -- quotes nothing and stays for good.

The action policy's records quote nothing either (M17, D8), with one
exception: a calendar write stores the exact request it sends, so that a
later attempt replays it rather than rebuilding it from current code. The
registry clears it once the write is done; the purge clears any left a week
after the write began. By then no attempt will replay it: a write still
unconfirmed is only looked up, by its event's id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import psycopg

from app.graph.checkpointer import postgres_checkpointer
from app.graph.nodes import NOT_A_MEETING, SWEEP_REASON
from app.jobs.poll import CLAIMED_NOT_RUN
from app.mail.feed import GONE, TOO_OLD
from app.policy import audit
from app.store.ledger import STRANDED_REASON, MessageStatus

FINAL_STATUSES = (MessageStatus.SKIPPED, MessageStatus.REJECTED, MessageStatus.CREATED)

FAILED_KEPT_FOR = timedelta(days=7)
REASONS_KEPT_FOR = timedelta(days=7)
CONTENT_KEPT_FOR = REASONS_KEPT_FOR
"""Proposal cards and the owner's corrections (M16, D8): the same week as the
ledger's reasons, counted on the same clock. The owner approved no other."""

REQUESTS_KEPT_FOR = timedelta(days=7)
"""A calendar write's stored request (M17, D8), counted from when it began."""

FIXED_REASONS = (
    "declined by user",
    "rejected by reviewer",
    SWEEP_REASON,
    STRANDED_REASON,
    CLAIMED_NOT_RUN,
    # The mail feed's (M20, D4): mail reached too late, mail deleted before
    # its turn, and the classifier's no in place of its reasoning.
    TOO_OLD,
    GONE,
    NOT_A_MEETING,
    # The action policy's (M17, D7): a refusal or an expiry, in its fixed words.
    *audit.REASONS.values(),
)
"""Ledger reasons written by code rather than by a model. They quote no email,
and they are what the failures view and M24's statistics are built from."""


@dataclass(frozen=True, slots=True)
class PurgeResult:
    threads: int
    """Threads whose checkpoints were deleted."""

    reasons_cleared: int
    """Ledger rows whose model-written text was removed or cut to its type."""

    proposals_cleared: int
    """Proposals whose card was removed."""

    corrections_cleared: int
    """Decisions whose correction was removed."""

    pairing_codes_deleted: int
    """Expired pairing codes."""

    mail_messages_deleted: int = 0
    """Mail sync rows past their 180 days, or a week gone (M20, D7)."""

    requests_cleared: int = 0
    """Stored calendar requests a week after their write began (M17, D8)."""


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

    reasons_cleared = _clear_reasons(conn)
    proposals_cleared, corrections_cleared = _clear_content(conn)
    pairing_codes_deleted = conn.execute(
        "DELETE FROM pairing_codes WHERE expires_at < now()"
    ).rowcount
    return PurgeResult(
        threads=len(thread_ids),
        reasons_cleared=reasons_cleared,
        proposals_cleared=proposals_cleared,
        corrections_cleared=corrections_cleared,
        pairing_codes_deleted=pairing_codes_deleted,
        mail_messages_deleted=_purge_mail(conn),
        requests_cleared=_clear_requests(conn),
    )


MAIL_KEPT_FOR = timedelta(days=180)
"""The owner's decision (M20, D7): mail metadata older than this is deleted."""

GONE_KEPT_FOR = timedelta(days=7)
"""A row whose message left the mailbox is kept this long after it went."""


def _purge_mail(conn: psycopg.Connection) -> int:
    """Delete mail sync rows past their 180 days, and rows a week gone.

    The ledger is untouched: it records what was done, and quotes nothing.
    """
    return conn.execute(
        "DELETE FROM gmail_messages WHERE internal_at < now() - %s OR gone_at < now() - %s",
        (MAIL_KEPT_FOR, GONE_KEPT_FOR),
    ).rowcount


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


def _clear_requests(conn: psycopg.Connection) -> int:
    """Clear the stored request of every write that began over a week ago."""
    return conn.execute(
        """
        UPDATE outbound_actions SET request = NULL
         WHERE request IS NOT NULL
           AND coalesce(started_at, created_at) < now() - %s
        """,
        (REQUESTS_KEPT_FOR,),
    ).rowcount


def _clear_content(conn: psycopg.Connection) -> tuple[int, int]:
    """Remove the cards and corrections of proposals settled over a week ago.

    Returns how many proposals and how many decisions lost their content.

    The clock is the ledger's: a final status or FAILED, a week old.
    Nothing is cleared while the proposal is open, whatever its ledger says:
    the owner can still decide a pending card, and the worker still owns a
    deciding one. An open decision is never cleared either. Timestamps are
    left alone, so M24's timings stay as they were.
    """
    settled = [status.value for status in (*FINAL_STATUSES, MessageStatus.FAILED)]
    proposals = conn.execute(
        """
        UPDATE proposals p SET payload = NULL
          FROM processed_messages m
         WHERE m.gmail_message_id = p.message_id
           AND p.payload IS NOT NULL
           AND p.status IN ('decided', 'failed')
           AND m.status = ANY(%s)
           AND m.updated_at < now() - %s
        """,
        (settled, CONTENT_KEPT_FOR),
    ).rowcount
    corrections = conn.execute(
        """
        UPDATE decisions d SET correction = NULL
          FROM proposals p
          JOIN processed_messages m ON m.gmail_message_id = p.message_id
         WHERE p.message_id = d.message_id
           AND d.correction IS NOT NULL
           AND d.outcome IS NOT NULL
           AND p.status IN ('decided', 'failed')
           AND m.status = ANY(%s)
           AND m.updated_at < now() - %s
        """,
        (settled, CONTENT_KEPT_FOR),
    ).rowcount
    return proposals, corrections
