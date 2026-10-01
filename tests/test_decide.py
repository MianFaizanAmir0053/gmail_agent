"""Recording a decision (M16, D1): validate, claim and enqueue, never touching
the graph. Since M17 a Confirm also carries what the owner saw -- the hash
prefix, the mode and the generation -- and records the approval it binds."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from typing import Any, cast

import psycopg
import pytest

from app.channel.decide import (
    MAX_CORRECTION_CHARS,
    DecisionResult,
    card_token,
    decide,
    parse_token,
)
from app.channel.park import proposal_from, write_park
from app.policy.hashing import INVITE, Binding, args_key
from app.store.ledger import MessageLedger, MessageStatus

PENDING: dict[str, Any] = {
    "proposed": {
        "is_meeting": True,
        "title": "Design review",
        "start_utc": "2026-10-05T11:00:00Z",
        "end_utc": "2026-10-05T12:00:00Z",
        "timezone": "Asia/Karachi",
        "attendees": ["sara@example.com"],
        "location": None,
        "confidence": 0.9,
        "reasoning": "Sara asked to meet",
    },
    "conflicts": [],
    "dry_run": True,
    "review_issues": [],
    "action_type": "calendar_invite",
    "pipeline_version": "0123456789ab",
}

BINDING = Binding(calendar_id="test-calendar", key=args_key("test-key"))

NO_DATABASE = cast(psycopg.Connection, object())
"""Any attribute access fails: proves validation refuses before touching Postgres."""


def _park(
    conn: psycopg.Connection,
    message_id: str = "m1",
    revision: int = 1,
    pending: dict[str, Any] = PENDING,
) -> None:
    MessageLedger(conn).claim(message_id, message_id)
    with conn.transaction():
        write_park(
            conn,
            proposal_from(message_id, pending, revision, BINDING),
            ledger_status=MessageStatus.CLAIMED,
        )


def _token(conn: psycopg.Connection, message_id: str = "m1") -> str:
    """The token the owner's card carries for the proposal as it is now."""
    row = conn.execute(
        "SELECT args_hash, dry_run, generation FROM proposals WHERE message_id = %s",
        (message_id,),
    ).fetchone()
    assert row is not None and row[0] is not None
    return card_token(row[0], row[1], row[2])


def _confirm(
    conn: psycopg.Connection, message_id: str = "m1", *, via: Any = "web", token: str | None = None
) -> DecisionResult:
    return decide(
        conn,
        message_id,
        action="confirm",
        revision=1,
        via=via,
        token=_token(conn, message_id) if token is None else token,
        dry_run=True,
    )


def _decisions(conn: psycopg.Connection, message_id: str = "m1") -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT revision, action, via, action_type, pipeline_version, correction, outcome "
        "FROM decisions WHERE message_id = %s ORDER BY id",
        (message_id,),
    ).fetchall()


def _actions(conn: psycopg.Connection, message_id: str = "m1") -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT tool, tier, args_hash, dry_run, status, length(nonce) "
        "FROM outbound_actions WHERE message_id = %s ORDER BY id",
        (message_id,),
    ).fetchall()


def _status(conn: psycopg.Connection, message_id: str = "m1") -> str:
    row = conn.execute(
        "SELECT status FROM proposals WHERE message_id = %s", (message_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


# --- the card's token -------------------------------------------------------


def test_a_token_names_the_hash_prefix_the_mode_and_the_generation() -> None:
    token = card_token("3f9a1c07be42" + "0" * 52, False, 2)
    assert token == "3f9a1c07be42-live-2"
    assert parse_token(token) == ("3f9a1c07be42", False, 2)
    assert parse_token(card_token("a" * 64, True, 1)) == ("a" * 12, True, 1)


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "3f9a1c07be42-live",
        "3f9a1c07be4-live-1",
        "3F9A1C07BE42-live-1",
        "x-dry-1",
        "3f9a1c07be42-maybe-1",
        "3f9a1c07be42-dry-0",
        "3f9a1c07be42-dry-1-extra",
    ],
)
def test_a_malformed_token_is_no_token(token: str | None) -> None:
    assert parse_token(token) is None


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


@pytest.mark.parametrize("token", [None, "not a token"])
def test_a_confirm_without_what_the_owner_saw_is_stale(token: str | None) -> None:
    """A card or button from before M17, or a mangled one, binds nothing."""
    result = decide(
        NO_DATABASE, "m1", action="confirm", revision=1, via="web", token=token, dry_run=True
    )
    assert result.status == "stale"


def test_a_confirm_needs_the_current_mode() -> None:
    """Every caller knows `DRY_RUN`; a Confirm without it is a programming error."""
    result = decide(
        NO_DATABASE, "m1", action="confirm", revision=1, via="web", token="a" * 12 + "-dry-1"
    )
    assert result.status == "invalid"


# --- claiming and enqueueing (Postgres) --------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("action", ["cancel", "sweep"])
def test_each_other_action_enqueues_exactly_one_decision(
    conn: psycopg.Connection, action: Any
) -> None:
    _park(conn)

    result = decide(conn, "m1", action=action, revision=1, via="cli")

    assert result.status == "queued"
    assert result.decision_id is not None
    assert _status(conn) == "deciding"
    assert _decisions(conn) == [(1, action, "cli", "calendar_invite", "0123456789ab", None, None)]
    assert _actions(conn) == []


@pytest.mark.integration
def test_a_confirm_enqueues_its_decision_and_the_approval_it_binds(
    conn: psycopg.Connection,
) -> None:
    _park(conn)
    stored = proposal_from("m1", PENDING, 1, BINDING)

    result = _confirm(conn, via="cli")

    assert result.status == "queued"
    assert _decisions(conn) == [
        (1, "confirm", "cli", "calendar_invite", "0123456789ab", None, None)
    ]
    assert _actions(conn) == [(INVITE, 2, stored.args_hash, True, "approved", 32)]
    audited = conn.execute(
        "SELECT kind, tool, tier, args_hash, dry_run FROM audit_log"
        " WHERE message_id = 'm1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert audited == ("action_approved", INVITE, 2, stored.args_hash, True)


@pytest.mark.integration
def test_an_edit_records_its_correction_and_binds_nothing(conn: psycopg.Connection) -> None:
    _park(conn)

    result = decide(conn, "m1", action="edit", revision=1, correction=" make it 5pm ", via="web")

    assert result.status == "queued"
    assert _decisions(conn)[0][5] == "make it 5pm"
    assert _actions(conn) == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "seen",
    [
        lambda token: "0" * 12 + token[12:],  # another hash: the proposal changed
        lambda token: token.replace("-dry-", "-live-"),  # another mode
        lambda token: token[:-1] + "2",  # another generation: a return or a Withdraw
    ],
)
def test_a_confirm_on_anything_but_what_the_owner_saw_is_stale(
    conn: psycopg.Connection, seen: Any
) -> None:
    _park(conn)

    result = _confirm(conn, token=seen(_token(conn)))

    assert result.status == "stale"
    assert _status(conn) == "pending"
    assert _decisions(conn) == []
    assert _actions(conn) == []


@pytest.mark.integration
def test_a_proposal_made_under_another_mode_cannot_be_confirmed(
    conn: psycopg.Connection,
) -> None:
    """Parked under dry run, it is never run live: the boot pass expires it."""
    _park(conn)

    result = decide(
        conn, "m1", action="confirm", revision=1, via="web", token=_token(conn), dry_run=False
    )

    assert result.status == "stale"
    assert "another mode" in result.detail
    assert _decisions(conn) == []


@pytest.mark.integration
def test_a_proposal_with_nothing_to_bind_is_not_ready(conn: psycopg.Connection) -> None:
    """Parked before M17, before reconciliation gave it a hash."""
    _park(conn)
    conn.execute("UPDATE proposals SET args_hash = NULL WHERE message_id = 'm1'")

    result = _confirm(conn, token="0" * 12 + "-dry-1")

    assert result.status == "not_ready"
    assert _decisions(conn) == []


@pytest.mark.integration
def test_a_stale_revision_is_refused_and_nothing_is_enqueued(conn: psycopg.Connection) -> None:
    _park(conn, revision=2)
    token = _token(conn)

    result = decide(conn, "m1", action="cancel", revision=1, via="web", token=token)

    assert result.status == "stale"
    assert result.current_revision == 2
    assert _status(conn) == "pending"
    assert _decisions(conn) == []


@pytest.mark.integration
def test_a_second_tap_on_the_same_card_is_refused(conn: psycopg.Connection) -> None:
    _park(conn)
    _confirm(conn)

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
def test_a_retried_confirm_gets_the_decision_and_the_approval_it_already_made(
    conn: psycopg.Connection,
) -> None:
    _park(conn)
    token = _token(conn)
    first = _confirm(conn, token=token)

    again = _confirm(conn, token=token)

    assert again.status == "queued"
    assert again.decision_id == first.decision_id
    assert len(_actions(conn)) == 1


@pytest.mark.integration
def test_decisions_outlive_any_attempt_to_delete_their_message(conn: psycopg.Connection) -> None:
    """M24's evidence: a reset or a stray DELETE must fail rather than erase it."""
    _park(conn)
    _confirm(conn)

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

    result = decide(conn, "m1", action="cancel", revision=1, via="cli")

    assert result.status == "not_found"


@pytest.mark.integration
def test_latency_is_measured_from_when_that_revision_parked(conn: psycopg.Connection) -> None:
    _park(conn)
    conn.execute(
        "UPDATE proposals SET parked_at = now() - interval '90 seconds' WHERE message_id = 'm1'"
    )

    _confirm(conn)

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


def _race(migrated_database: str, message_id: str, actions: tuple[str, str]) -> list[Any]:
    """Two taps on one card, each on its own connection, released together."""
    barrier = threading.Barrier(2)
    results: list[DecisionResult] = []
    lock = threading.Lock()
    with psycopg.connect(migrated_database) as read:
        token = _token(read, message_id)

    def tap(action: Any) -> None:
        with psycopg.connect(migrated_database, autocommit=True) as own:
            barrier.wait()
            result = decide(
                own,
                message_id,
                action=action,
                revision=1,
                via="web",
                token=token,
                dry_run=True,
            )
        with lock:
            results.append(result)

    threads = [threading.Thread(target=tap, args=(a,)) for a in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@pytest.mark.integration
def test_two_concurrent_confirms_record_one_decision_and_one_approval(
    migrated_database: str, committed_proposal: str
) -> None:
    """The second is answered as a retry of the first (M17, D2)."""
    results = _race(migrated_database, committed_proposal, ("confirm", "confirm"))

    assert [r.status for r in results] == ["queued", "queued"]
    assert len({r.decision_id for r in results}) == 1
    with psycopg.connect(migrated_database) as check:
        assert len(_decisions(check, committed_proposal)) == 1
        assert len(_actions(check, committed_proposal)) == 1


@pytest.mark.integration
def test_two_concurrent_decisions_on_one_revision_one_wins(
    migrated_database: str, committed_proposal: str
) -> None:
    results = _race(migrated_database, committed_proposal, ("confirm", "cancel"))

    assert sorted(r.status for r in results) == ["queued", "stale"]
    with psycopg.connect(migrated_database) as check:
        decisions = _decisions(check, committed_proposal)
        assert len(decisions) == 1
        # An approval exists exactly when the Confirm won.
        assert len(_actions(check, committed_proposal)) == (decisions[0][1] == "confirm")
