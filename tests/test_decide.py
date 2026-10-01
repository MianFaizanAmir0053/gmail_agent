"""Recording a decision (M16, D1): validate, claim and enqueue, never touching the graph."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from typing import Any, cast

import psycopg
import pytest

from app.channel.decide import MAX_CORRECTION_CHARS, DecisionResult, decide
from app.channel.park import proposal_from, write_park
from app.store.ledger import MessageLedger, MessageStatus

PENDING: dict[str, Any] = {
    "proposed": {"title": "Design review", "attendees": ["sara@example.com"]},
    "conflicts": [],
    "dry_run": True,
    "review_issues": [],
    "action_type": "calendar_invite",
    "pipeline_version": "0123456789ab",
}

NO_DATABASE = cast(psycopg.Connection, object())
"""Any attribute access fails: proves validation refuses before touching Postgres."""


def _park(conn: psycopg.Connection, message_id: str = "m1", revision: int = 1) -> None:
    MessageLedger(conn).claim(message_id, message_id)
    with conn.transaction():
        write_park(
            conn,
            proposal_from(message_id, PENDING, revision),
            ledger_status=MessageStatus.CLAIMED,
        )


def _decisions(conn: psycopg.Connection, message_id: str = "m1") -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT revision, action, via, action_type, pipeline_version, correction, outcome "
        "FROM decisions WHERE message_id = %s ORDER BY id",
        (message_id,),
    ).fetchall()


def _status(conn: psycopg.Connection, message_id: str = "m1") -> str:
    row = conn.execute(
        "SELECT status FROM proposals WHERE message_id = %s", (message_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


# --- validation, before anything moves --------------------------------------


@pytest.mark.parametrize("correction", ["", "   "])
def test_an_edit_without_a_correction_is_refused(correction: str) -> None:
    """The graph would route it to `reject` and log it as a human decline."""
    result = decide(NO_DATABASE, "m1", action="edit", revision=1, correction=correction, via="web")
    assert result.status == "invalid"


def test_an_edit_at_revision_three_is_refused() -> None:
    """The graph would reject a third edit silently (MAX_REVISIONS = 2)."""
    result = decide(NO_DATABASE, "m1", action="edit", revision=3, correction="later", via="web")
    assert result.status == "invalid"


def test_an_overlong_correction_is_refused() -> None:
    too_long = "x" * (MAX_CORRECTION_CHARS + 1)
    result = decide(NO_DATABASE, "m1", action="edit", revision=1, correction=too_long, via="web")
    assert result.status == "invalid"


def test_a_correction_postgres_cannot_store_is_refused() -> None:
    result = decide(NO_DATABASE, "m1", action="edit", revision=1, correction="5\x00pm", via="web")
    assert result.status == "invalid"


@pytest.mark.parametrize(
    ("action", "via"),
    [
        ("approve", "web"),  # not an action
        ("confirm", "email"),  # not a channel
        ("sweep", "web"),  # a sweep is an operator's, never a tap
        ("sweep", "telegram"),
        ("confirm", "sweep"),  # the sweep path only sweeps
    ],
)
def test_an_action_or_channel_outside_the_contract_is_refused(action: str, via: str) -> None:
    """Callers are typed, but the CLI and the API hand over strings."""
    result = decide(NO_DATABASE, "m1", action=cast(Any, action), revision=1, via=cast(Any, via))
    assert result.status == "invalid"


# --- claiming and enqueueing (Postgres) --------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("action", ["confirm", "cancel", "sweep"])
def test_each_action_enqueues_exactly_one_decision(conn: psycopg.Connection, action: Any) -> None:
    _park(conn)

    result = decide(conn, "m1", action=action, revision=1, via="cli")

    assert result.status == "queued"
    assert result.decision_id is not None
    assert _status(conn) == "deciding"
    assert _decisions(conn) == [(1, action, "cli", "calendar_invite", "0123456789ab", None, None)]


@pytest.mark.integration
def test_an_edit_records_its_correction(conn: psycopg.Connection) -> None:
    _park(conn)

    result = decide(conn, "m1", action="edit", revision=1, correction=" make it 5pm ", via="web")

    assert result.status == "queued"
    assert _decisions(conn)[0][5] == "make it 5pm"


@pytest.mark.integration
def test_a_stale_revision_is_refused_and_nothing_is_enqueued(conn: psycopg.Connection) -> None:
    _park(conn, revision=2)

    result = decide(conn, "m1", action="confirm", revision=1, via="web")

    assert result.status == "stale"
    assert result.current_revision == 2
    assert _status(conn) == "pending"
    assert _decisions(conn) == []


@pytest.mark.integration
def test_a_second_tap_on_the_same_card_is_refused(conn: psycopg.Connection) -> None:
    _park(conn)
    decide(conn, "m1", action="confirm", revision=1, via="web")

    result = decide(conn, "m1", action="cancel", revision=1, via="web")

    assert result.status == "stale"
    assert len(_decisions(conn)) == 1


@pytest.mark.integration
def test_a_retried_request_gets_the_decision_it_already_made(conn: psycopg.Connection) -> None:
    """A lost response followed by a retry must not read as someone else's tap."""
    _park(conn)
    first = decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")

    again = decide(conn, "m1", action="edit", revision=1, correction=" make it 5pm", via="web")

    assert again.status == "queued"
    assert again.decision_id == first.decision_id
    assert len(_decisions(conn)) == 1


@pytest.mark.integration
def test_decisions_outlive_any_attempt_to_delete_their_message(conn: psycopg.Connection) -> None:
    """M24's evidence: a reset or a stray DELETE must fail rather than erase it."""
    _park(conn)
    decide(conn, "m1", action="confirm", revision=1, via="web")

    # Which error depends on the server: Postgres 16 (CI) reports RESTRICT as a
    # foreign-key violation, and Postgres 18 (Neon) as a restrict violation.
    # Either way the delete fails, and that is the guarantee.
    refused = (psycopg.errors.ForeignKeyViolation, psycopg.errors.RestrictViolation)
    with pytest.raises(refused), conn.transaction():
        conn.execute("DELETE FROM processed_messages WHERE gmail_message_id = 'm1'")

    assert len(_decisions(conn)) == 1


@pytest.mark.integration
def test_a_message_with_no_proposal_is_not_found(conn: psycopg.Connection) -> None:
    MessageLedger(conn).claim("m1", "m1")

    result = decide(conn, "m1", action="confirm", revision=1, via="cli")

    assert result.status == "not_found"


@pytest.mark.integration
def test_latency_is_measured_from_when_that_revision_parked(conn: psycopg.Connection) -> None:
    _park(conn)
    conn.execute(
        "UPDATE proposals SET parked_at = now() - interval '90 seconds' WHERE message_id = 'm1'"
    )

    decide(conn, "m1", action="confirm", revision=1, via="web")

    row = conn.execute("SELECT latency_seconds FROM decisions WHERE message_id = 'm1'").fetchone()
    assert row is not None
    assert 89 <= row[0] <= 120


# --- two taps at once (real concurrency) --------------------------------------


@pytest.fixture
def committed_proposal(migrated_database: str) -> Iterator[str]:
    """A parked proposal committed for real, so two connections can race on it."""
    message_id = f"race-{uuid.uuid4().hex[:12]}"
    with psycopg.connect(migrated_database, autocommit=True) as setup:
        _park(setup, message_id)
    yield message_id
    with psycopg.connect(migrated_database, autocommit=True) as cleanup:
        # Decisions and their approvals are protected from cascading deletes,
        # so this test's own rows go first, explicitly.
        cleanup.execute("DELETE FROM outbound_actions WHERE message_id = %s", (message_id,))
        cleanup.execute("DELETE FROM decisions WHERE message_id = %s", (message_id,))
        cleanup.execute("DELETE FROM processed_messages WHERE gmail_message_id = %s", (message_id,))


@pytest.mark.integration
def test_two_concurrent_decisions_on_one_revision_one_wins(
    migrated_database: str, committed_proposal: str
) -> None:
    barrier = threading.Barrier(2)
    results: list[DecisionResult] = []
    lock = threading.Lock()

    def tap(action: Any) -> None:
        with psycopg.connect(migrated_database, autocommit=True) as own:
            barrier.wait()
            result = decide(own, committed_proposal, action=action, revision=1, via="web")
        with lock:
            results.append(result)

    threads = [threading.Thread(target=tap, args=(a,)) for a in ("confirm", "cancel")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(r.status for r in results) == ["queued", "stale"]
    with psycopg.connect(migrated_database) as check:
        assert len(_decisions(check, committed_proposal)) == 1
