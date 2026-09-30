"""Ledger behaviour against a real Postgres. Skipped when none is reachable."""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from app.store.ledger import (
    STRANDED_REASON,
    MessageLedger,
    MessageStatus,
    StatusTransitionError,
    SyncCursor,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def ledger(conn: psycopg.Connection) -> MessageLedger:
    return MessageLedger(conn)


# --- the point of the whole module -----------------------------------------


def test_claiming_twice_fails_the_second_time(ledger: MessageLedger) -> None:
    """Re-running the poller must not re-process. This is M04 in one assertion."""
    assert ledger.claim("m1", "t1") is True
    assert ledger.claim("m1", "t1") is False


def test_claim_is_atomic_across_connections(migrated_database: str) -> None:
    """Two pollers racing on the same inbox: exactly one may win.

    A check-then-insert would let both read "not present" before either wrote.
    """
    with (
        psycopg.connect(migrated_database) as first,
        psycopg.connect(migrated_database) as second,
    ):
        try:
            assert MessageLedger(first).claim("race1", "t1") is True
            first.commit()
            assert MessageLedger(second).claim("race1", "t1") is False
        finally:
            first.execute("DELETE FROM processed_messages WHERE gmail_message_id = 'race1'")
            first.commit()


def test_unseen_filters_out_known_messages(ledger: MessageLedger) -> None:
    ledger.claim("m1", "t1")
    assert ledger.unseen(["m1", "m2", "m3"]) == ["m2", "m3"]


def test_unseen_preserves_input_order(ledger: MessageLedger) -> None:
    ledger.claim("m2", "t1")
    assert ledger.unseen(["m3", "m2", "m1"]) == ["m3", "m1"]


def test_unseen_handles_an_empty_batch(ledger: MessageLedger) -> None:
    assert ledger.unseen([]) == []


# --- status transitions ----------------------------------------------------


def test_created_requires_an_event_id(ledger: MessageLedger) -> None:
    ledger.claim("m1", "t1")
    with pytest.raises(StatusTransitionError, match="requires a calendar_event_id"):
        ledger.mark("m1", MessageStatus.CREATED)


def test_non_created_statuses_reject_an_event_id(ledger: MessageLedger) -> None:
    ledger.claim("m1", "t1")
    with pytest.raises(StatusTransitionError, match="must not carry"):
        ledger.mark("m1", MessageStatus.SKIPPED, calendar_event_id="evt_1")


def test_the_database_enforces_the_same_rule(conn: psycopg.Connection) -> None:
    """Belt and braces: the CHECK constraint holds even if code bypasses the API."""
    MessageLedger(conn).claim("m1", "t1")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE processed_messages SET status = 'skipped', calendar_event_id = 'evt_1' "
            "WHERE gmail_message_id = 'm1'"
        )


def test_marking_an_unclaimed_message_is_an_error(ledger: MessageLedger) -> None:
    with pytest.raises(StatusTransitionError, match="claim it first"):
        ledger.mark("never-seen", MessageStatus.SKIPPED)


def test_created_records_the_event_id(ledger: MessageLedger) -> None:
    ledger.claim("m1", "t1")
    ledger.mark("m1", MessageStatus.CREATED, calendar_event_id="evt_1")

    entry = ledger.get("m1")
    assert entry is not None
    assert entry.status is MessageStatus.CREATED
    assert entry.calendar_event_id == "evt_1"
    assert entry.is_terminal


def test_failed_is_not_terminal_so_it_can_be_retried(ledger: MessageLedger) -> None:
    """Failures are usually transient. Terminal would mean hand-editing rows."""
    ledger.claim("m1", "t1")
    ledger.mark("m1", MessageStatus.FAILED, error="rate limited")

    entry = ledger.get("m1")
    assert entry is not None
    assert entry.is_terminal is False
    assert entry.error == "rate limited"


def test_rejected_is_terminal(ledger: MessageLedger) -> None:
    ledger.claim("m1", "t1")
    ledger.mark("m1", MessageStatus.REJECTED)

    entry = ledger.get("m1")
    assert entry is not None
    assert entry.is_terminal


def test_marking_bumps_updated_at(ledger: MessageLedger) -> None:
    ledger.claim("m1", "t1")
    before = ledger.get("m1")
    ledger.mark("m1", MessageStatus.EXTRACTED)
    after = ledger.get("m1")

    assert before is not None and after is not None
    assert after.updated_at >= before.updated_at
    assert after.created_at == before.created_at


def test_get_returns_none_for_unknown_message(ledger: MessageLedger) -> None:
    assert ledger.get("nope") is None


def test_counts_by_status(ledger: MessageLedger) -> None:
    ledger.claim("m1", "t1")
    ledger.claim("m2", "t2")
    ledger.mark("m2", MessageStatus.SKIPPED)

    counts = ledger.counts_by_status()
    assert counts[MessageStatus.CLAIMED] == 1
    assert counts[MessageStatus.SKIPPED] == 1


# --- sync cursor -----------------------------------------------------------


def test_cursor_starts_empty(conn: psycopg.Connection) -> None:
    assert SyncCursor(conn).get() is None


def test_cursor_round_trips(conn: psycopg.Connection) -> None:
    cursor = SyncCursor(conn)
    cursor.set("998877")
    assert cursor.get() == "998877"


def test_cursor_overwrites_rather_than_accumulating(conn: psycopg.Connection) -> None:
    cursor = SyncCursor(conn)
    cursor.set("1")
    cursor.set("2")

    assert cursor.get() == "2"
    assert conn.execute("SELECT count(*) FROM sync_state").fetchone() == (1,)


def test_sync_state_cannot_gain_a_second_row(conn: psycopg.Connection) -> None:
    """Two rows would mean two pollers disagreeing about where they are."""
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO sync_state (id, last_history_id) VALUES (2, 'x')")


# --- stranded claims (M15) -------------------------------------------------


def test_boot_recovery_fails_only_stale_claims(
    ledger: MessageLedger, conn: psycopg.Connection
) -> None:
    """A redeploy can kill a poll mid-message. `unseen` never offers that
    message again, so without recovery its `claimed` row is invisible forever."""
    ledger.claim("stale", "stale")
    ledger.claim("fresh", "fresh")
    ledger.claim("done", "done")
    ledger.mark("done", MessageStatus.SKIPPED)
    conn.execute(
        "UPDATE processed_messages SET updated_at = now() - interval '2 hours'"
        " WHERE gmail_message_id IN ('stale', 'done')"
    )

    recovered = ledger.fail_stranded(timedelta(hours=1))

    assert recovered == 1
    stale = ledger.get("stale")
    assert stale is not None
    assert stale.status == MessageStatus.FAILED
    assert stale.error == STRANDED_REASON


def test_boot_recovery_leaves_recent_claims_and_other_statuses_alone(
    ledger: MessageLedger, conn: psycopg.Connection
) -> None:
    ledger.claim("fresh", "fresh")
    ledger.claim("done", "done")
    ledger.mark("done", MessageStatus.SKIPPED)
    conn.execute(
        "UPDATE processed_messages SET updated_at = now() - interval '2 hours'"
        " WHERE gmail_message_id = 'done'"
    )

    ledger.fail_stranded(timedelta(hours=1))

    fresh, done = ledger.get("fresh"), ledger.get("done")
    assert fresh is not None and fresh.status == MessageStatus.CLAIMED
    assert done is not None and done.status == MessageStatus.SKIPPED
