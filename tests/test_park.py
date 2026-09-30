"""The park step (M16, D2): a parked thread's rows are written together or not at all."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from app.channel import park
from app.channel.park import ParkConflictError, ProposalRecord, proposal_from, record_park
from app.graph.runner import GraphSession
from app.store.ledger import MessageLedger, MessageStatus

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "007_proposals.sql"

PENDING: dict[str, Any] = {
    "message_id": "m1",
    "proposed": {
        "is_meeting": True,
        "title": "Design review",
        "start_utc": "2026-10-01T09:00:00Z",
        "end_utc": "2026-10-01T10:00:00Z",
        "timezone": "Asia/Karachi",
        "attendees": ["sara@example.com"],
        "location": None,
        "confidence": 0.9,
        "reasoning": "Sara wrote: let's meet Thursday at two",
    },
    "conflicts": ["Overlaps Standup"],
    "dry_run": True,
    "review_issues": ["the stated zone was ignored"],
    "action_type": "calendar_invite",
    "pipeline_version": "0123456789ab",
}

LEGACY: dict[str, Any] = {
    "message_id": "m1",
    "proposed": {**PENDING["proposed"], "attendees": []},
    "conflicts": [],
}


# --- building the record ----------------------------------------------------


def test_a_record_carries_the_payloads_fields_and_the_threads_revision() -> None:
    record = proposal_from("m1", PENDING, revision=2)

    assert record.revision == 2
    assert record.action_type == "calendar_invite"
    assert record.pipeline_version == "0123456789ab"
    assert record.dry_run is True
    assert record.payload["title"] == "Design review"
    assert record.payload["conflicts"] == ["Overlaps Standup"]
    assert record.payload["review_issues"] == ["the stated zone was ignored"]


def test_the_stored_payload_keeps_only_what_the_card_shows() -> None:
    """The model's reasoning can quote the email; the card never needs it."""
    record = proposal_from("m1", PENDING, revision=1)

    assert "reasoning" not in record.payload
    assert "confidence" not in record.payload
    assert "Thursday at two" not in str(record.payload)


def test_a_legacy_payload_is_filled_in_and_marked_pre_m16() -> None:
    record = proposal_from("m1", LEGACY, revision=1)

    assert record.action_type == "calendar_hold"
    assert record.pipeline_version == "pre-m16"
    assert record.payload["review_issues"] == []
    # Nobody wrote down the mode it was parked under, so it is treated as a
    # dry run: M17 must never act for real on it.
    assert record.dry_run is True


# --- writing it (Postgres) ---------------------------------------------------


@dataclass
class FakeSession:
    conn: psycopg.Connection
    revisions: dict[str, int] = field(default_factory=dict)

    def revision(self, message_id: str) -> int:
        return self.revisions.get(message_id, 1)


def _session(conn: psycopg.Connection) -> GraphSession:
    return cast(GraphSession, FakeSession(conn))


def _proposal(conn: psycopg.Connection, message_id: str = "m1") -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT status, revision, action_type, pipeline_version, dry_run FROM proposals "
        "WHERE message_id = %s",
        (message_id,),
    ).fetchone()


@pytest.mark.integration
def test_the_migration_can_be_run_again(conn: psycopg.Connection) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)


@pytest.mark.integration
def test_a_park_writes_the_proposal_and_marks_the_ledger(conn: psycopg.Connection) -> None:
    ledger = MessageLedger(conn)
    ledger.claim("m1", "m1")
    announced: list[str] = []

    record_park(
        _session(conn), "m1", PENDING, announce=lambda record: announced.append(record.message_id)
    )

    entry = ledger.get("m1")
    assert entry is not None
    assert entry.status is MessageStatus.AWAITING_APPROVAL
    assert _proposal(conn) == ("pending", 1, "calendar_invite", "0123456789ab", True)
    assert announced == ["m1"]


@pytest.mark.integration
def test_a_failure_between_the_writes_leaves_neither(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = MessageLedger(conn)
    ledger.claim("m1", "m1")

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(park, "_upsert_proposal", broken)

    with pytest.raises(RuntimeError):
        record_park(_session(conn), "m1", PENDING)

    entry = ledger.get("m1")
    assert entry is not None
    assert entry.status is MessageStatus.CLAIMED
    assert _proposal(conn) is None


@pytest.mark.integration
def test_a_park_never_overwrites_a_final_ledger_status(conn: psycopg.Connection) -> None:
    ledger = MessageLedger(conn)
    ledger.claim("m1", "m1")
    ledger.mark("m1", MessageStatus.CREATED, calendar_event_id="evt_1")

    with pytest.raises(ParkConflictError):
        record_park(_session(conn), "m1", PENDING)

    entry = ledger.get("m1")
    assert entry is not None
    assert entry.status is MessageStatus.CREATED
    assert entry.calendar_event_id == "evt_1"
    assert _proposal(conn) is None


@pytest.mark.integration
def test_a_failed_announcement_keeps_the_park(conn: psycopg.Connection) -> None:
    """The proposal is durably parked; telling the owner is best effort."""
    MessageLedger(conn).claim("m1", "m1")

    def failing(record: ProposalRecord) -> None:
        raise RuntimeError("push service down")

    record_park(_session(conn), "m1", PENDING, announce=failing)

    row = _proposal(conn)
    assert row is not None
    assert row[0] == "pending"


@pytest.mark.integration
def test_a_repark_moves_a_deciding_proposal_to_the_new_revision(
    conn: psycopg.Connection,
) -> None:
    ledger = MessageLedger(conn)
    ledger.claim("m1", "m1")
    record_park(_session(conn), "m1", PENDING)
    conn.execute("UPDATE proposals SET status = 'deciding' WHERE message_id = 'm1'")

    with conn.transaction():
        park.write_park(
            conn,
            proposal_from("m1", PENDING, revision=2),
            ledger_status=MessageStatus.AWAITING_APPROVAL,
        )

    row = _proposal(conn)
    assert row is not None
    assert row[:2] == ("pending", 2)


@pytest.mark.integration
def test_the_park_step_refuses_to_run_outside_a_transaction(migrated_database: str) -> None:
    """Outside one, the ledger write would commit before the proposal write."""
    with (
        psycopg.connect(migrated_database, autocommit=True) as bare,
        pytest.raises(RuntimeError, match="transaction"),
    ):
        park.write_park(
            bare,
            proposal_from("nobody", PENDING, revision=1),
            ledger_status=MessageStatus.CLAIMED,
        )


@pytest.mark.integration
def test_a_park_does_not_reopen_a_decided_proposal(conn: psycopg.Connection) -> None:
    ledger = MessageLedger(conn)
    ledger.claim("m1", "m1")
    record_park(_session(conn), "m1", PENDING)
    conn.execute(
        "UPDATE proposals SET status = 'decided', final_status = 'skipped' WHERE message_id = 'm1'"
    )

    with pytest.raises(ParkConflictError), conn.transaction():
        park.write_park(
            conn,
            proposal_from("m1", PENDING, revision=2),
            ledger_status=MessageStatus.AWAITING_APPROVAL,
        )

    row = _proposal(conn)
    assert row is not None
    assert row[:2] == ("decided", 1)
