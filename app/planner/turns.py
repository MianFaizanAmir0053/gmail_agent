"""A chat turn's record (M19, D1): stored by the API, and run once by the chat job.

The web app makes an idempotency key for each request, unique in
`chat_turns`. Its server action gives up after ten seconds and may send the
same request again; the second send finds the turn already stored, and never
makes another. A key sent again with other words is refused: those are
another request, which would otherwise be lost without a word.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

MAX_TYPED_CHARS = 2_000
"""The longest request the owner may type (D1)."""

MAX_PASTED_CHARS = 16_000
"""The most pasted text a turn keeps: the reader's room for a body (D6)."""


class KeyReusedError(Exception):
    """The key stored a turn with other words."""


@dataclass(frozen=True, slots=True)
class Stored:
    turn_id: int
    new: bool
    """False when the key was stored before: the request was sent again."""
    ended: bool = False
    """For a request sent again, whether its turn has ended: then nothing new
    is asked, and the owner must be told so."""


ENDED = ("answered", "stopped", "failed")


def store(conn: psycopg.Connection, *, key: str, typed: str, pasted: str | None) -> Stored:
    """Store a request, typed and pasted apart, or find the one this key stored
    with the same words. Raises `KeyReusedError` for other words."""
    row = conn.execute(
        """
        INSERT INTO chat_turns (idempotency_key, typed, pasted) VALUES (%s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        (key, typed, pasted),
    ).fetchone()
    if row is not None:
        return Stored(int(row[0]), new=True)
    found = conn.execute(
        "SELECT id, typed, pasted, status FROM chat_turns WHERE idempotency_key = %s", (key,)
    ).fetchone()
    if found is None:  # the key's row was removed between the two statements
        raise RuntimeError("the turn could not be stored")
    if (found[1], found[2]) != (typed, pasted):
        raise KeyReusedError
    return Stored(int(found[0]), new=False, ended=found[3] in ENDED)
