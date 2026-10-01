"""What a message is, and how it is kept (M20, D1).

One row per message in `gmail_messages`, from metadata only: labels, the
time Gmail received or sent it, addresses, and the raw bulk signals. Never a
subject, a snippet or a body -- they can carry one-time codes, and wait for
M18.

**What is stored.** Messages labelled `SENT`, and every other message except
Promotions and Social: Primary, Updates (where deadlines and money often
arrive, for M21) and Forums. Drafts, chats, spam and trash are not.

**What a label change recomputes.** `direction` and `category` follow the
labels. `to_self` comes from the headers, so moving a conversation to the
Inbox -- which labels the owner's own replies `INBOX` too -- changes nothing.
`offered_since`, the feed recall's stall clock, starts when a row begins to
meet the feed's rule and is cleared when it stops: reading the message, which
the rule does not look at, leaves it alone.

Storing is idempotent: storing the same message twice changes nothing, not
even `updated_at`, and how a row first arrived is kept for diagnosis.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from email.utils import getaddresses
from typing import Literal

import psycopg

from app.google.gmail import MessageMeta
from app.jobs.measure import normalise_address
from app.mail import feed

Direction = Literal["in", "out"]
Category = Literal["primary", "updates", "forums", "promotions", "social"]
ArrivedVia = Literal["history", "switch_over", "catch_up", "recall", "queue", "backfill"]

_OTHER_CATEGORIES: tuple[tuple[str, Category], ...] = (
    ("CATEGORY_PROMOTIONS", "promotions"),
    ("CATEGORY_SOCIAL", "social"),
    ("CATEGORY_UPDATES", "updates"),
    ("CATEGORY_FORUMS", "forums"),
)
"""The four tabs other than Primary. Gmail lists a message in a tab by that
tab's label, so any of these means the message is not in Primary."""

NOT_STORED_CATEGORIES = frozenset({"promotions", "social"})

NEVER_STORED = frozenset({"DRAFT", "CHAT", "SPAM", "TRASH"})

D1_QUERY = "-category:promotions -category:social -in:chats -in:drafts"
"""What D1 stores, as a Gmail search, for the catch-up, the backfill and the
sync recall. Spam and trash are never listed, so they need no term."""

PRIMARY_QUERY = (
    "-category:promotions -category:social -category:updates -category:forums -in:chats -in:drafts"
)
"""Primary, as Gmail lists it, for the recall and `--check-feed`: never
`category:primary`, which leaves out mail with no category label at all."""


def category_of(labels: frozenset[str]) -> Category:
    """`primary` for Primary, or for no category label at all."""
    for label, category in _OTHER_CATEGORIES:
        if label in labels:
            return category
    return "primary"


OUTBOUND = frozenset({"SENT", "SCHEDULED"})
"""Labels the owner's own mail carries. A scheduled send may carry SCHEDULED
without SENT until it goes; it is outbound all the same."""


def direction_of(labels: frozenset[str]) -> Direction:
    return "out" if labels & OUTBOUND else "in"


def kept(labels: frozenset[str]) -> bool:
    """Whether D1 stores a message with these labels."""
    if labels & NEVER_STORED:
        return False
    return "SENT" in labels or category_of(labels) not in NOT_STORED_CATEGORIES


def owner_addresses(*addresses: str) -> frozenset[str]:
    """Every address that reaches the owner, normalised the way Gmail reads it:
    `OWNER_EMAIL`, `OWNER_ALIASES` and the mailbox's own address."""
    return frozenset(normalise_address(address) for address in addresses if address.strip())


def _addresses(raw: str) -> tuple[str, ...]:
    return tuple(address.lower() for _, address in getaddresses([raw]) if address)


@dataclass(frozen=True, slots=True)
class MessageRow:
    """One `gmail_messages` row, as classified from a message's metadata."""

    message_id: str
    thread_id: str
    internal_at: datetime
    label_ids: tuple[str, ...]
    """Sorted, so the same labels always compare equal."""

    direction: Direction
    to_self: bool
    category: Category
    from_addr: str | None
    to_addrs: tuple[str, ...]
    cc_addrs: tuple[str, ...]
    has_list_unsubscribe: bool
    """Whether the header was there. Its URL is never kept: it can carry a
    token that unsubscribes the owner."""

    precedence: str | None
    auto_submitted: str | None

    @property
    def kept(self) -> bool:
        return kept(frozenset(self.label_ids))


def classify(meta: MessageMeta, owners: frozenset[str]) -> MessageRow:
    """A message's row. `owners` comes from `owner_addresses`.

    `to_self` is mail from the owner to the owner, read from `From` and `To`:
    a forward to yourself would otherwise be fed as if someone else had sent
    it. A copy in `Cc` does not count.

    NUL characters are dropped from every header first: Postgres text cannot
    hold one, so a row carrying it could never be stored.
    """
    headers = {name: value.replace("\x00", "") for name, value in meta.headers.items()}
    senders = _addresses(headers.get("From", ""))
    to_addrs = _addresses(headers.get("To", ""))
    from_addr = senders[0] if senders else None
    to_self = (
        from_addr is not None
        and normalise_address(from_addr) in owners
        and any(normalise_address(address) in owners for address in to_addrs)
    )
    return MessageRow(
        message_id=meta.id,
        thread_id=meta.thread_id,
        internal_at=meta.internal_date,
        label_ids=tuple(sorted(meta.label_ids)),
        direction=direction_of(meta.label_ids),
        to_self=to_self,
        category=category_of(meta.label_ids),
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs=_addresses(headers.get("Cc", "")),
        has_list_unsubscribe=bool(headers.get("List-Unsubscribe", "").strip()),
        precedence=headers.get("Precedence", "").strip().lower() or None,
        auto_submitted=headers.get("Auto-Submitted", "").strip().lower() or None,
    )


StoreOutcome = Literal["inserted", "updated", "unchanged", "not_kept"]


def store(
    conn: psycopg.Connection, account: str, row: MessageRow, arrived_via: ArrivedVia
) -> StoreOutcome:
    """Store a message D1 keeps, or bring a stored one's labels up to date.

    A message D1 leaves out is not inserted -- but if it is already stored
    (moved to the trash, say), its row takes its current labels: rows are
    kept current, never deleted for a label. A successful fetch is evidence
    the message is there, so it clears `gone_at`.

    The headers' fields and how the row first arrived are written once.
    """
    if not row.kept:
        if not _refresh(conn, account, row):
            return "not_kept"
        _clock(conn, account, row.message_id)
        return "updated"
    found = conn.execute(
        """
        INSERT INTO gmail_messages
            (account, message_id, thread_id, internal_at, label_ids, direction, to_self,
             category, from_addr, to_addrs, cc_addrs, has_list_unsubscribe, precedence,
             auto_submitted, arrived_via)
        VALUES (%(account)s, %(message_id)s, %(thread_id)s, %(internal_at)s, %(label_ids)s,
                %(direction)s, %(to_self)s, %(category)s, %(from_addr)s, %(to_addrs)s,
                %(cc_addrs)s, %(has_list_unsubscribe)s, %(precedence)s, %(auto_submitted)s,
                %(arrived_via)s)
        ON CONFLICT (account, message_id) DO UPDATE
           SET label_ids = EXCLUDED.label_ids,
               direction = EXCLUDED.direction,
               category = EXCLUDED.category,
               gone_at = NULL,
               updated_at = now()
         WHERE gmail_messages.label_ids IS DISTINCT FROM EXCLUDED.label_ids
            OR gmail_messages.gone_at IS NOT NULL
        RETURNING (xmax = 0)
        """,
        {
            "account": account,
            "message_id": row.message_id,
            "thread_id": row.thread_id,
            "internal_at": row.internal_at,
            "label_ids": list(row.label_ids),
            "direction": row.direction,
            "to_self": row.to_self,
            "category": row.category,
            "from_addr": row.from_addr,
            "to_addrs": list(row.to_addrs),
            "cc_addrs": list(row.cc_addrs),
            "has_list_unsubscribe": row.has_list_unsubscribe,
            "precedence": row.precedence,
            "auto_submitted": row.auto_submitted,
            "arrived_via": arrived_via,
        },
    ).fetchone()
    if found is None:
        return "unchanged"
    _clock(conn, account, row.message_id)
    return "inserted" if found[0] else "updated"


def _refresh(conn: psycopg.Connection, account: str, row: MessageRow) -> bool:
    return bool(
        conn.execute(
            """
            UPDATE gmail_messages
               SET label_ids = %s, direction = %s, category = %s,
                   gone_at = NULL, updated_at = now()
             WHERE account = %s AND message_id = %s
               AND (label_ids IS DISTINCT FROM %s OR gone_at IS NOT NULL)
            """,
            (
                list(row.label_ids),
                row.direction,
                row.category,
                account,
                row.message_id,
                list(row.label_ids),
            ),
        ).rowcount
    )


LabelOutcome = Literal["absent", "changed", "unchanged"]


def apply_labels(
    conn: psycopg.Connection,
    account: str,
    message_id: str,
    *,
    added: frozenset[str] = frozenset(),
    removed: frozenset[str] = frozenset(),
) -> LabelOutcome:
    """Apply a history record's label additions and removals, with no fetch.

    `absent` means the message is not stored, and the caller decides whether
    the change could bring it in. A change never clears `gone_at`: history is
    read in order, so a deletion recorded after it is read after it too.
    """
    found = conn.execute(
        """
        SELECT label_ids FROM gmail_messages
         WHERE account = %s AND message_id = %s
           FOR UPDATE
        """,
        (account, message_id),
    ).fetchone()
    if found is None:
        return "absent"
    before = frozenset(found[0])
    labels = (before | added) - removed
    if labels == before:
        return "unchanged"
    conn.execute(
        """
        UPDATE gmail_messages
           SET label_ids = %s, direction = %s, category = %s, updated_at = now()
         WHERE account = %s AND message_id = %s
        """,
        (sorted(labels), direction_of(labels), category_of(labels), account, message_id),
    )
    _clock(conn, account, message_id)
    return "changed"


def mark_gone(conn: psycopg.Connection, account: str, message_id: str) -> bool:
    """Record that a message left the mailbox: history reported it deleted,
    or a fetch answered 404. Never for not being listed -- archived or
    recategorised mail is not gone. False if not stored, or already gone."""
    marked = conn.execute(
        """
        UPDATE gmail_messages SET gone_at = now(), updated_at = now()
         WHERE account = %s AND message_id = %s AND gone_at IS NULL
        """,
        (account, message_id),
    ).rowcount
    if marked:
        _clock(conn, account, message_id)
    return bool(marked)


def _clock(conn: psycopg.Connection, account: str, message_id: str) -> None:
    """Keep the row's stall clock (`offered_since`) after its labels or
    `gone_at` changed: started when it begins to meet the feed's rule, kept
    while it still does, cleared when it stops. The rule's own SQL decides,
    so the two cannot drift apart."""
    conn.execute(
        f"""
        UPDATE gmail_messages m
           SET offered_since = CASE WHEN {feed.ROW} THEN coalesce(m.offered_since, now()) END
         WHERE m.account = %s AND m.message_id = %s
        """,
        (account, message_id),
    )


def stored_ids(conn: psycopg.Connection, account: str, message_ids: Iterable[str]) -> set[str]:
    """Which of these messages have a row, gone or not."""
    wanted = list(message_ids)
    if not wanted:
        return set()
    rows = conn.execute(
        "SELECT message_id FROM gmail_messages WHERE account = %s AND message_id = ANY(%s)",
        (account, wanted),
    ).fetchall()
    return {row[0] for row in rows}
