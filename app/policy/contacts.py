"""Confirmed contacts (M17, D4).

A guest outside the thread may be invited once the owner allows them, and the
Allow is kept until the owner removes it. Addresses are stored as
`participants.guest_key` spells them, so an Allow covers the mailbox however
a later proposal writes it.

Every change is audited with a keyed hash of the address in `subject_hash`:
the row links to the contact without naming it (D7).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal, Protocol

import psycopg

from app.google.gmail import MessageGoneError
from app.policy import audit
from app.policy.hashing import subject_hash
from app.policy.participants import SPACE, guest_key, outside, participants

__all__ = [
    "MAX_ADDRESS_CHARS",
    "allow",
    "confirmed",
    "guest_key",
    "remove",
    "unconfirmed_outsiders",
]

Via = Literal["web", "cli"]

MAX_ADDRESS_CHARS = 320
"""The longest address the mail standards allow: 64 characters, `@`, 255."""


def allow(
    conn: psycopg.Connection,
    address: str,
    *,
    via: Via,
    key: bytes,
    message_id: str | None = None,
) -> None:
    """Confirm a contact. Allowing one already confirmed changes nothing."""
    contact = _checked(address)
    with conn.transaction():
        added = conn.execute(
            """
            INSERT INTO confirmed_contacts (address, via, message_id) VALUES (%s, %s, %s)
            ON CONFLICT (address) DO NOTHING
            """,
            (contact, via, message_id),
        ).rowcount
        if added:
            audit.record(
                conn,
                "contact_allowed",
                message_id=message_id,
                subject_hash=subject_hash(key, contact),
            )


def remove(conn: psycopg.Connection, address: str, *, key: bytes) -> bool:
    """Remove a confirmed contact. True if there was one."""
    contact = _checked(address)
    with conn.transaction():
        removed = conn.execute(
            "DELETE FROM confirmed_contacts WHERE address = %s", (contact,)
        ).rowcount
        if removed:
            audit.record(conn, "contact_removed", subject_hash=subject_hash(key, contact))
    return bool(removed)


def confirmed(conn: psycopg.Connection, addresses: Iterable[str]) -> frozenset[str]:
    """The guest keys, among `addresses`, of contacts the owner allowed."""
    keys = sorted({guest_key(address) for address in addresses})
    if not keys:
        return frozenset()
    rows = conn.execute(
        "SELECT address FROM confirmed_contacts WHERE address = ANY(%s)", (keys,)
    ).fetchall()
    return frozenset(row[0] for row in rows)


class _Message(Protocol):
    @property
    def thread_id(self) -> str: ...


class ThreadReader(Protocol):
    def message_metadata(self, message_id: str) -> _Message: ...

    def thread_headers(self, thread_id: str) -> dict[str, Any]: ...


def unconfirmed_outsiders(
    conn: psycopg.Connection, gmail: ThreadReader, message_id: str, guests: Sequence[str]
) -> list[str]:
    """The guests who are neither in the message's thread nor confirmed
    contacts, as given, in order.

    Read now, from Gmail and the contacts, never from a graph's state: the
    worker's check before a Confirm and the registry's at execution both use
    it (D4). The thread is the message's own, as Gmail files it: the
    ledger's `thread_id` holds the message id, never the thread's. Raises
    when Gmail cannot be read; a message or thread Gmail no longer has reads
    as an empty thread, so every guest not allowed is outside. Guests who are
    all allowed need no read at all, so Gmail being down never holds them.
    """
    allowed = confirmed(conn, guests)
    unknown = [guest for guest in guests if guest_key(guest) not in allowed]
    if not unknown:
        return []
    try:
        thread = gmail.thread_headers(gmail.message_metadata(message_id).thread_id)
    except MessageGoneError:
        thread = {"messages": []}
    return outside(unknown, participants=participants(thread), confirmed=frozenset())


def _checked(address: str) -> str:
    """The guest key, for anything shaped like one address: printable ASCII,
    no space, none of `<>,;` -- the web app checks the same (`isAddress`)."""
    address = address.strip(SPACE)
    local, at, domain = address.partition("@")
    if (
        not at
        or not local
        or "." not in domain
        or len(address) > MAX_ADDRESS_CHARS
        or any(not "!" <= c <= "~" or c in "<>,;" for c in address)
    ):
        raise ValueError("not an email address")
    return guest_key(address)
