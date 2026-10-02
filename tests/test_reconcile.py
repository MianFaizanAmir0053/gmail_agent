"""Reconciliation (M16, D3): every live interrupt has a row, and rows follow the ledger.

The threads are faked -- what reconciliation reads from a checkpoint is a
`ThreadView` -- and the rows are real Postgres, because the rules are about
which rows exist in which state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import psycopg
import pytest

from app.channel.decide import card_token, decide
from app.channel.park import proposal_from, write_park
from app.channel.reconcile import CLAIM_GRACE, reconcile
from app.graph.runner import GraphSession, ThreadView
from app.policy.hashing import Binding, args_key
from app.store.ledger import MessageLedger, MessageStatus

PAYLOAD: dict[str, Any] = {
    "proposed": {"title": "Design review", "attendees": ["sara@example.com"]},
    "conflicts": [],
    "dry_run": True,
    "review_issues": [],
    "action_type": "calendar_invite",
    "pipeline_version": "0123456789ab",
}

LEGACY: dict[str, Any] = {"proposed": {"title": "Old one", "attendees": []}, "conflicts": []}


@dataclass
class FakeSession:
    conn: psycopg.Connection
    threads: dict[str, ThreadView] = field(default_factory=dict)
    dry_run: bool = True

    def thread(self, message_id: str) -> ThreadView:
        return self.threads.get(message_id, ThreadView(payload=None, revision=1, next=()))

    def revision(self, message_id: str) -> int:
        return self.thread(message_id).revision

    def binding(self) -> Binding:
        return Binding(calendar_id="test-calendar", key=args_key("test-key"))

    def parks(self, message_id: str, payload: dict[str, Any], revision: int = 1) -> None:
        self.threads[message_id] = ThreadView(
            payload=payload, revision=revision, next=("await_approval",)
        )


def _session(conn: psycopg.Connection) -> FakeSession:
    return FakeSession(conn)


def _run(
    session: FakeSession, announced: list[str] | None = None, *, bind_and_expire: bool = True
) -> Any:
    """As the scheduler runs it, M17's passes included, unless told not to."""
    sink = announced if announced is not None else []
    return reconcile(
        cast(GraphSession, session),
        announce=lambda record: sink.append(record.message_id),
        bind_and_expire=bind_and_expire,
    )


def _ledger_row(conn: psycopg.Connection, message_id: str, status: MessageStatus, age: str) -> None:
    MessageLedger(conn).claim(message_id, message_id)
    if status is not MessageStatus.CLAIMED:
        conn.execute(
            "UPDATE processed_messages SET status = %s WHERE gmail_message_id = %s",
            (status.value, message_id),
        )
    conn.execute(
        "UPDATE processed_messages SET updated_at = now() - %s::interval "
        "WHERE gmail_message_id = %s",
        (age, message_id),
    )


def _proposal(conn: psycopg.Connection, message_id: str) -> tuple[Any, ...] | None:
    row = conn.execute(
        "SELECT status, revision, pipeline_version, final_status FROM proposals "
        "WHERE message_id = %s",
        (message_id,),
    ).fetchone()
    return None if row is None else tuple(row)


def _ledger(conn: psycopg.Connection, message_id: str) -> MessageStatus:
    entry = MessageLedger(conn).get(message_id)
    assert entry is not None
    return entry.status


# --- a live interrupt always has a row -----------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("status", "age"),
    [
        (MessageStatus.CLAIMED, "1 hour"),  # crashed between the checkpoint and the park step
        (MessageStatus.AWAITING_APPROVAL, "1 minute"),  # parked before M16
        (MessageStatus.FAILED, "2 days"),  # boot's fail_stranded, or poll's dead letter
    ],
)
def test_a_live_interrupt_without_a_row_gets_one(
    conn: psycopg.Connection, status: MessageStatus, age: str
) -> None:
    session = _session(conn)
    _ledger_row(conn, "m1", status, age)
    session.parks("m1", PAYLOAD, revision=2)
    announced: list[str] = []

    result = _run(session, announced)

    assert result.recorded == 1
    assert _proposal(conn, "m1") == ("pending", 2, "0123456789ab", None)
    assert _ledger(conn, "m1") is MessageStatus.AWAITING_APPROVAL
    assert announced == ["m1"]


@pytest.mark.integration
def test_a_legacy_park_is_recorded_as_pre_m16(conn: psycopg.Connection) -> None:
    session = _session(conn)
    _ledger_row(conn, "m1", MessageStatus.AWAITING_APPROVAL, "3 days")
    session.parks("m1", LEGACY, revision=3)

    _run(session)

    assert _proposal(conn, "m1") == ("pending", 3, "pre-m16", None)


@pytest.mark.integration
def test_a_claim_a_poll_may_still_be_parking_is_left_alone(conn: psycopg.Connection) -> None:
    session = _session(conn)
    _ledger_row(conn, "m1", MessageStatus.CLAIMED, "1 minute")
    session.parks("m1", PAYLOAD)

    assert _run(session).recorded == 0
    assert _proposal(conn, "m1") is None
    assert CLAIM_GRACE.total_seconds() > 60


@pytest.mark.integration
def test_a_failure_older_than_its_checkpoint_is_not_read(conn: psycopg.Connection) -> None:
    """The purge removes a FAILED thread's checkpoint after seven days."""
    session = _session(conn)
    _ledger_row(conn, "m1", MessageStatus.FAILED, "8 days")
    session.parks("m1", PAYLOAD)

    assert _run(session).recorded == 0


@pytest.mark.integration
def test_a_thread_that_is_not_parked_gets_nothing(conn: psycopg.Connection) -> None:
    session = _session(conn)
    _ledger_row(conn, "m1", MessageStatus.FAILED, "1 hour")

    assert _run(session).recorded == 0
    assert _ledger(conn, "m1") is MessageStatus.FAILED


# --- rows follow the ledger ------------------------------------------------------


def _parked_with_row(conn: psycopg.Connection, session: FakeSession, message_id: str) -> None:
    MessageLedger(conn).claim(message_id, message_id)
    session.parks(message_id, PAYLOAD)
    with conn.transaction():
        write_park(conn, proposal_from(message_id, PAYLOAD, 1), ledger_status=MessageStatus.CLAIMED)


@pytest.mark.integration
@pytest.mark.parametrize("proposal_status", ["pending", "failed"])
def test_a_row_whose_ledger_is_final_is_closed_as_decided(
    conn: psycopg.Connection, proposal_status: str
) -> None:
    """For example, a proposal the M15 CLI decided directly."""
    session = _session(conn)
    _parked_with_row(conn, session, "m1")
    conn.execute("UPDATE proposals SET status = %s WHERE message_id = 'm1'", (proposal_status,))
    MessageLedger(conn).mark("m1", MessageStatus.REJECTED, error="declined by user")
    session.threads.pop("m1")  # the thread ended

    result = _run(session)

    assert result.closed == 1
    assert _proposal(conn, "m1") == ("decided", 1, "0123456789ab", "rejected")


@pytest.mark.integration
def test_a_deciding_proposal_is_never_touched(conn: psycopg.Connection) -> None:
    session = _session(conn)
    _parked_with_row(conn, session, "m1")
    decide(conn, "m1", action="cancel", revision=1, via="web")
    MessageLedger(conn).mark("m1", MessageStatus.SKIPPED, error="dry_run")
    session.threads.pop("m1")

    result = _run(session)

    assert (result.recorded, result.closed) == (0, 0)
    assert _proposal(conn, "m1") == ("deciding", 1, "0123456789ab", None)


@pytest.mark.integration
def test_a_proposal_that_is_still_parked_is_not_closed(conn: psycopg.Connection) -> None:
    """A final ledger beside a live interrupt is contradictory; leave it be --
    but counted, so the tick is not reported healthy and a person looks."""
    session = _session(conn)
    _parked_with_row(conn, session, "m1")
    MessageLedger(conn).mark("m1", MessageStatus.REJECTED, error="declined by user")

    result = _run(session)

    assert (result.closed, result.errors) == (0, 1)
    assert _proposal(conn, "m1") == ("pending", 1, "0123456789ab", None)


# --- every pending proposal can be bound, and runs as it was made (M17, D2) ------

TIMED: dict[str, Any] = {
    **PAYLOAD,
    "proposed": {
        "is_meeting": True,
        "title": "Design review",
        "start_utc": "2026-10-05T11:00:00Z",
        "end_utc": "2026-10-05T12:00:00Z",
        "timezone": "Asia/Karachi",
        "attendees": ["sara@example.com"],
        "location": None,
        "confidence": 0.9,
        "reasoning": "r",
    },
}


def _parked_before_m17(conn: psycopg.Connection, payload: dict[str, Any] = TIMED) -> None:
    """A pending proposal written before M17: no tool, no hash."""
    MessageLedger(conn).claim("m1", "m1")
    with conn.transaction():
        write_park(conn, proposal_from("m1", payload, 1), ledger_status=MessageStatus.CLAIMED)


@pytest.mark.integration
def test_a_proposal_parked_before_m17_is_bound_and_can_then_be_confirmed(
    conn: psycopg.Connection,
) -> None:
    session = _session(conn)
    _parked_before_m17(conn)
    session.parks("m1", TIMED)

    assert _run(session).bound == 1

    row = conn.execute(
        "SELECT tool, args_hash, dry_run, generation FROM proposals WHERE message_id = 'm1'"
    ).fetchone()
    assert row is not None and row[0] == "calendar.create_invite" and row[1] is not None
    confirmed = decide(
        conn,
        "m1",
        action="confirm",
        revision=1,
        via="web",
        token=card_token(row[1], row[2], row[3]),
        dry_run=row[2],
    )
    assert confirmed.status == "queued"


@pytest.mark.integration
def test_a_proposal_whose_thread_moved_on_is_not_bound(conn: psycopg.Connection) -> None:
    """Only while still pending at the revision read."""
    session = _session(conn)
    _parked_before_m17(conn)
    session.parks("m1", TIMED, revision=2)

    assert _run(session).bound == 0


@pytest.mark.integration
def test_a_proposal_made_under_the_other_mode_is_expired(conn: psycopg.Connection) -> None:
    """DRY_RUN went off: a proposal made under dry run is ended by a sweep,
    never shown as if it could run."""
    session = _session(conn)
    session.dry_run = False
    _parked_before_m17(conn, PAYLOAD)
    session.parks("m1", PAYLOAD)

    assert _run(session).expired == 1

    row = conn.execute(
        "SELECT action, via, reason FROM decisions WHERE message_id = 'm1'"
    ).fetchone()
    assert row == ("sweep", "sweep", "made under another mode")


@pytest.mark.integration
def test_a_proposal_made_under_this_mode_is_left_alone(conn: psycopg.Connection) -> None:
    session = _session(conn)
    _parked_before_m17(conn, PAYLOAD)
    session.parks("m1", PAYLOAD)

    assert _run(session).expired == 0


@pytest.mark.integration
def test_a_command_line_run_neither_binds_nor_expires(conn: psycopg.Connection) -> None:
    """It runs under its own DRY_RUN, calendar and key, not production's: a
    local `approve --reconcile` must never expire live proposals."""
    session = _session(conn)
    session.dry_run = False
    _parked_before_m17(conn)
    session.parks("m1", TIMED)

    result = _run(session, bind_and_expire=False)

    assert (result.bound, result.expired) == (0, 0)
    assert conn.execute("SELECT count(*) FROM decisions").fetchone() == (0,)


@pytest.mark.integration
def test_a_command_line_run_leaves_a_missing_row_to_the_scheduler(
    conn: psycopg.Connection,
) -> None:
    """Under the command line's own settings, a hash could be one production
    refuses, and a card from the other mode could be pushed as live. So it
    records nothing, and says so; the scheduler's next pass records the row,
    binds it and announces it, as it would have anyway."""
    session = _session(conn)
    _ledger_row(conn, "m1", MessageStatus.CLAIMED, "1 hour")
    session.parks("m1", TIMED)
    announced: list[str] = []

    by_hand = _run(session, announced, bind_and_expire=False)

    assert (by_hand.recorded, by_hand.left) == (0, 1)
    assert _proposal(conn, "m1") is None
    assert announced == []

    scheduled = _run(session, announced)

    assert scheduled.recorded == 1
    assert announced == ["m1"]


@pytest.mark.integration
def test_a_proposal_whose_message_is_final_is_closed_not_expired(
    conn: psycopg.Connection,
) -> None:
    """Left behind by the M15 CLI: no sweep, and no expiry in the audit log."""
    session = _session(conn)
    session.dry_run = False
    _parked_before_m17(conn, PAYLOAD)
    MessageLedger(conn).mark("m1", MessageStatus.REJECTED)

    result = _run(session)

    assert (result.closed, result.expired) == (1, 0)
    assert conn.execute("SELECT count(*) FROM decisions").fetchone() == (0,)


@pytest.mark.integration
def test_a_proposal_parked_under_the_other_mode_is_never_announced(
    conn: psycopg.Connection,
) -> None:
    """Recorded, then expired in the same pass: the owner is not pushed a card
    that is gone a moment later."""
    session = _session(conn)
    session.dry_run = False
    _ledger_row(conn, "m1", MessageStatus.AWAITING_APPROVAL, "1 minute")
    session.parks("m1", PAYLOAD)
    announced: list[str] = []

    result = _run(session, announced)

    assert (result.recorded, result.expired) == (1, 1)
    assert announced == []


@pytest.mark.integration
def test_a_proposal_decided_while_it_was_being_bound_is_left_alone(
    conn: psycopg.Connection,
) -> None:
    """The update checks the row again: a Confirm or Cancel recorded after
    the read must not have the row bound under it."""

    @dataclass
    class Racing(FakeSession):
        def thread(self, message_id: str) -> ThreadView:
            self.conn.execute("UPDATE proposals SET status = 'deciding' WHERE message_id = 'm1'")
            return super().thread(message_id)

    session = Racing(conn)
    _parked_before_m17(conn)
    session.parks("m1", TIMED)

    assert _run(session).bound == 0
    assert conn.execute("SELECT args_hash FROM proposals WHERE message_id = 'm1'").fetchone() == (
        None,
    )
