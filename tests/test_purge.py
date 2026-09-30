"""Checkpoint retention (M15).

Every polled message's full body -- one-time-code mail included -- sits in its
graph checkpoint, and nothing used to delete it. The purge keeps what a
pending decision still needs, and nothing else.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import TypedDict

import psycopg
import pytest
from langgraph.graph import END, START, StateGraph

from app.graph.checkpointer import postgres_checkpointer
from app.graph.nodes import SWEEP_REASON
from app.jobs.purge import purge
from app.store.ledger import STRANDED_REASON, MessageLedger, MessageStatus

pytestmark = pytest.mark.integration


class _State(TypedDict):
    x: int


def _checkpoint(database_url: str, thread_id: str) -> None:
    """Leave a real checkpoint for `thread_id`, as a graph run would."""
    builder: StateGraph[_State, None, _State, _State] = StateGraph(_State)
    builder.add_node("n", lambda state: {"x": state["x"] + 1})
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    with postgres_checkpointer(database_url) as saver:
        builder.compile(checkpointer=saver).invoke(
            {"x": 0}, {"configurable": {"thread_id": thread_id}}
        )


def _has_checkpoint(conn: psycopg.Connection, thread_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM checkpoints WHERE thread_id = %s LIMIT 1", (thread_id,))
    return row.fetchone() is not None


def _age(conn: psycopg.Connection, message_id: str, days: int) -> None:
    conn.execute(
        "UPDATE processed_messages SET updated_at = now() - make_interval(days => %s)"
        " WHERE gmail_message_id = %s",
        (days, message_id),
    )


@pytest.fixture
def ids(migrated_database: str) -> Iterator[dict[str, str]]:
    """Unique thread ids: checkpoints are written on their own connection and
    outlive the test's rolled-back transaction, so they are removed at the end."""
    names = ("skipped", "parked", "failed_new", "failed_old", "claimed")
    made = {name: f"{name}-{uuid.uuid4().hex[:8]}" for name in names}
    yield made
    with postgres_checkpointer(migrated_database) as saver:
        for thread_id in made.values():
            saver.delete_thread(thread_id)


def _ledger(conn: psycopg.Connection, ids: dict[str, str]) -> None:
    ledger = MessageLedger(conn)
    for name, message_id in ids.items():
        ledger.claim(message_id, message_id)
        status = {
            "skipped": MessageStatus.SKIPPED,
            "parked": MessageStatus.AWAITING_APPROVAL,
            "failed_new": MessageStatus.FAILED,
            "failed_old": MessageStatus.FAILED,
            "claimed": None,
        }[name]
        if status is not None:
            ledger.mark(message_id, status, error="quoted: your code is 482913")
    _age(conn, ids["failed_old"], days=8)


def test_finished_threads_lose_their_checkpoints_and_pending_ones_keep_them(
    conn: psycopg.Connection, migrated_database: str, ids: dict[str, str]
) -> None:
    for thread_id in ids.values():
        _checkpoint(migrated_database, thread_id)
    _ledger(conn, ids)

    purge(conn, migrated_database)

    assert not _has_checkpoint(conn, ids["skipped"])
    assert not _has_checkpoint(conn, ids["failed_old"])
    assert _has_checkpoint(conn, ids["parked"])  # a decision still needs it
    assert _has_checkpoint(conn, ids["failed_new"])  # kept a week for diagnosis
    assert _has_checkpoint(conn, ids["claimed"])  # in flight


def test_model_written_reasons_are_cleared_after_a_week(
    conn: psycopg.Connection, migrated_database: str, ids: dict[str, str]
) -> None:
    """`skip` stores the model's reasoning, which quotes the email it read."""
    _ledger(conn, ids)
    _age(conn, ids["skipped"], days=8)

    purge(conn, migrated_database)

    entry = MessageLedger(conn).get(ids["skipped"])
    assert entry is not None and entry.error is None


def test_fixed_operator_reasons_survive(
    conn: psycopg.Connection, migrated_database: str, ids: dict[str, str]
) -> None:
    """These are what M24's statistics and M15's evidence are built from."""
    ledger = MessageLedger(conn)
    kept = {}
    for reason in ("declined by user", SWEEP_REASON, STRANDED_REASON):
        message_id = f"kept-{uuid.uuid4().hex[:8]}"
        ledger.claim(message_id, message_id)
        status = MessageStatus.FAILED if reason == STRANDED_REASON else MessageStatus.REJECTED
        ledger.mark(message_id, status, error=reason)
        _age(conn, message_id, days=30)
        kept[message_id] = reason

    purge(conn, migrated_database)

    for message_id, reason in kept.items():
        entry = ledger.get(message_id)
        assert entry is not None and entry.error == reason
