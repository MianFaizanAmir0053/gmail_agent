"""The durable checkpointer, with an explicit type allowlist.

Graph state holds Pydantic models from `app.contracts`. LangGraph's serializer
will happily write and read them, but on the way back it logs:

    Deserializing unregistered type app.contracts.EmailMessage from checkpoint.
    This will be blocked in a future version.

That is a real time bomb: every checkpoint we write depends on a path that is
scheduled to be closed, so a routine dependency bump would break resume for
every parked approval at once. Registering the three types costs nothing and
turns a future outage into a no-op upgrade.

It is also a security boundary. The serializer can construct arbitrary objects
from checkpoint rows, so anyone who can write to the checkpoint tables could
otherwise trigger code execution on deserialize. The allowlist bounds that to
three known models.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row

CHECKPOINT_TYPES = [
    ("app.contracts", "EmailMessage"),
    ("app.contracts", "ExtractionResult"),
    ("app.contracts", "ActionResult"),
]
"""Everything `GraphState` can hold. Adding a model to the state means adding it
here, or resume breaks under `LANGGRAPH_STRICT_MSGPACK`."""


def checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)


@contextmanager
def postgres_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    """A checkpointer on its own connection.

    Deliberately not sharing the ledger's connection: the saver issues its own
    queries around node boundaries, and interleaving them with application
    transactions on one connection invites surprises that only appear under
    load.

    `from_conn_string` is not used because it gives no way to pass a serializer.
    """
    # `dict_row` is required, not stylistic: PostgresSaver reads its rows by
    # column name, and a default tuple-row connection fails inside the saver.
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        saver = PostgresSaver(conn, serde=checkpoint_serde())
        saver.setup()  # idempotent
        yield saver
