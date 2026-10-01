"""The meeting pipeline's feed (M20, D4).

Poll's candidates are stored rows that meet all of these:
- strictly inbound Primary mail: `direction` `in`, `category` `primary`, not
  `to_self` -- a forward to yourself would duplicate the original's proposal;
- not labelled SPAM or TRASH, and not gone;
- not bulk: no `Precedence: bulk` or `junk`, no `Auto-Submitted` other than
  `no`, and, for a message with no category label at all, no
  `List-Unsubscribe`;
- `internal_at` at or after `feed_from` less an hour;
- no ledger row yet.

Mailing lists in Primary stay in: team mail from Google Groups carries
`List-Unsubscribe` and `Precedence: list`, and meeting requests arrive that
way. Where Gmail has categorised the mailbox, promotional bulk mail has
already left Primary; where it has not, `List-Unsubscribe` is the marker.

The feed decides by time, never by how a row arrived. And a row waiting on
the fetch queue is held back: a catch-up queues the week's rows to be
fetched again, and until that answers, their labels may be stale -- a
message trashed during an outage would otherwise be fed.

A candidate is processed only while it is under seven days old. An older
one is recorded `SKIPPED` ("too old when reached") without a model call, so
nothing that meets the rule is ever silently dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

from app.store.ledger import STRANDED_REASON, MessageLedger, MessageStatus

log = logging.getLogger(__name__)

AGE_LIMIT = timedelta(days=7)
"""The owner's decision: mail first reached later than this is recorded, not processed."""

MARGIN = timedelta(hours=1)
"""The feed starts this long before `feed_from`, so the old poller's last
hour is covered twice rather than not at all. The ledger keeps a message
that both reached from being processed twice."""

TOO_OLD = "too old when reached"
GONE = "no longer in the mailbox"
"""Fixed ledger reasons: written by code, quoting no email, kept by the purge."""

_OFFERED = """
        m.direction = 'in' AND m.category = 'primary' AND NOT m.to_self
    AND m.gone_at IS NULL
    AND NOT (m.label_ids && ARRAY['SPAM', 'TRASH'])
    AND (m.precedence IS NULL OR m.precedence NOT IN ('bulk', 'junk'))
    AND (m.auto_submitted IS NULL OR m.auto_submitted = 'no')
    AND (NOT m.has_list_unsubscribe OR 'CATEGORY_PERSONAL' = ANY(m.label_ids))
    AND m.internal_at >= c.feed_from - %(margin)s
"""
"""What the feed would offer, ledger aside: `m` is the row, `c` its cursor."""

_WAITING = """
    AND NOT EXISTS (SELECT 1 FROM processed_messages p
                     WHERE p.gmail_message_id = m.message_id)
    AND NOT EXISTS (SELECT 1 FROM gmail_fetch_queue q
                     WHERE q.message_id = m.message_id AND q.status = 'queued')
"""
"""No ledger row yet, and not held back for a fetch."""

RULE = _OFFERED + _WAITING
"""The whole rule, for the feed and for the recall and `--check-feed`
checks that ask whether it held: the same SQL, so they cannot drift apart.
Its one parameter is `margin`."""


def active(conn: psycopg.Connection) -> bool:
    """Whether the feed has started: the first sync run made a cursor. Until
    then poll keeps reading the newest unread page, as it did before M20."""
    row = conn.execute("SELECT EXISTS (SELECT 1 FROM gmail_cursors)").fetchone()
    return bool(row and row[0])


def candidates(conn: psycopg.Connection, limit: int, *, now: datetime | None = None) -> list[str]:
    """Up to `limit` messages for the pipeline, oldest first."""
    rows = conn.execute(
        f"""
        SELECT m.message_id
          FROM gmail_messages m
          JOIN gmail_cursors c ON c.account = m.account
         WHERE {RULE}
           AND m.internal_at >= %(fresh)s
         ORDER BY m.internal_at, m.message_id
         LIMIT %(limit)s
        """,
        {"margin": MARGIN, "fresh": (now or datetime.now(UTC)) - AGE_LIMIT, "limit": limit},
    ).fetchall()
    return [row[0] for row in rows]


def record_too_old(conn: psycopg.Connection, *, now: datetime | None = None) -> list[str]:
    """Give every candidate over seven days old a `SKIPPED` ledger row, with
    no model call. Returns the messages recorded.

    That covers mail found by a catch-up after a long outage, mail restored
    from the trash, and mail held by M17's pause or spending cap for a week.
    The ledger's thread id is the message id, as `claim` writes it.
    """
    rows = conn.execute(
        f"""
        INSERT INTO processed_messages (gmail_message_id, thread_id, status, error)
        SELECT m.message_id, m.message_id, %(skipped)s, %(reason)s
          FROM gmail_messages m
          JOIN gmail_cursors c ON c.account = m.account
         WHERE {RULE}
           AND m.internal_at < %(fresh)s
        ON CONFLICT (gmail_message_id) DO NOTHING
        RETURNING gmail_message_id
        """,
        {
            "margin": MARGIN,
            "fresh": (now or datetime.now(UTC)) - AGE_LIMIT,
            "skipped": MessageStatus.SKIPPED.value,
            "reason": TOO_OLD,
        },
    ).fetchall()
    return [row[0] for row in rows]


# --- claims stranded by a restart ---------------------------------------------


@dataclass(frozen=True, slots=True)
class StrandedClaims:
    left: int
    """Threads that parked: reconciliation records them (M16, D3)."""

    released: int
    """Deleted, ledger row and checkpoint, so the feed offers them again."""

    failed: int
    """Marked FAILED, as before M20: the feed would not offer them again."""


def recover_stranded(
    conn: psycopg.Connection,
    *,
    parked: Callable[[str], bool],
    forget: Callable[[str], None],
    now: datetime | None = None,
) -> StrandedClaims:
    """Settle every `claimed` row. Call at boot, when no run is in flight.

    A claim is taken before its graph runs and replaced by the graph's own
    outcome, so one still `claimed` at boot was left by a process that died
    mid-message. A thread that parked is left to reconciliation. One that did
    not is released -- its checkpoint (`forget`) and then its ledger row are
    deleted -- when the feed would offer it again: stored, meeting the rule,
    and under seven days old. Anything else is marked FAILED, so nothing is
    dropped without a trace.
    """
    rows = conn.execute(
        f"""
        SELECT p.gmail_message_id,
               EXISTS (SELECT 1
                         FROM gmail_messages m
                         JOIN gmail_cursors c ON c.account = m.account
                        WHERE m.message_id = p.gmail_message_id
                          AND {_OFFERED}
                          AND m.internal_at >= %(fresh)s)
          FROM processed_messages p
         WHERE p.status = %(claimed)s
         ORDER BY p.created_at
        """,
        {
            "margin": MARGIN,
            "fresh": (now or datetime.now(UTC)) - AGE_LIMIT,
            "claimed": MessageStatus.CLAIMED.value,
        },
    ).fetchall()
    ledger = MessageLedger(conn)
    left = released = failed = 0
    for message_id, offered in rows:
        if parked(message_id):
            left += 1
        elif offered:
            # The checkpoint first: a crash between the two leaves a claim with
            # no thread, which the next boot releases again.
            forget(message_id)
            released += ledger.release(message_id)
        else:
            ledger.mark(message_id, MessageStatus.FAILED, error=STRANDED_REASON)
            failed += 1
    return StrandedClaims(left=left, released=released, failed=failed)
