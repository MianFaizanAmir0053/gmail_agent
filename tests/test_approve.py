"""The operator's CLI over proposals (M16: it records decisions and waits).

Since M16 the CLI never resumes a thread: it records a decision through
`decide()`, and the worker in the app applies it. What is under test is which
decisions get recorded, and how the wait for their outcome behaves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import psycopg
import pytest

from app.channel.park import proposal_from, write_park
from app.graph.runner import GraphSession
from app.jobs import approve
from app.jobs.approve import decide_current, list_pending, sweep_all, wait_for
from app.store.ledger import MessageLedger, MessageStatus

PENDING: dict[str, Any] = {
    "proposed": {
        "title": "Design review",
        "start_utc": "2026-10-02T11:00:00Z",
        "end_utc": "2026-10-02T12:00:00Z",
        "attendees": ["sara@example.com"],
    },
    "conflicts": ["Overlaps Standup"],
    "dry_run": True,
    "action_type": "calendar_invite",
    "pipeline_version": "0123456789ab",
}


# --- waiting for the worker ------------------------------------------------------


@dataclass
class Clock:
    now: float = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_the_wait_ends_with_the_outcome_once_the_worker_settles_it() -> None:
    answers = iter([None, None, "skipped"])
    clock = Clock()

    outcome = wait_for(
        lambda: next(answers), timeout=60, poll_every=2, sleep=clock.sleep, clock=clock.time
    )

    assert outcome == "skipped"
    assert clock.now == 4


def test_the_wait_gives_up_at_its_timeout_rather_than_hanging() -> None:
    """With no app running, nothing will ever apply the decision."""
    clock = Clock()

    outcome = wait_for(lambda: None, timeout=10, poll_every=2, sleep=clock.sleep, clock=clock.time)

    assert outcome is None
    assert clock.now == 10


# --- recording decisions (Postgres) ----------------------------------------------


def _park(conn: psycopg.Connection, message_id: str, revision: int = 1) -> None:
    MessageLedger(conn).claim(message_id, message_id)
    with conn.transaction():
        write_park(
            conn,
            proposal_from(message_id, PENDING, revision),
            ledger_status=MessageStatus.CLAIMED,
        )


def _decisions(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT message_id, revision, action, via FROM decisions ORDER BY id"
    ).fetchall()


@pytest.mark.integration
def test_a_decision_is_made_on_the_proposals_current_revision(conn: psycopg.Connection) -> None:
    """The operator names a message, not a revision: they act on what is shown."""
    _park(conn, "m1", revision=2)

    result = decide_current(conn, "m1", action="confirm")

    assert result.status == "queued"
    assert _decisions(conn) == [("m1", 2, "confirm", "cli")]


@pytest.mark.integration
def test_a_message_with_no_proposal_is_not_found(conn: psycopg.Connection) -> None:
    assert decide_current(conn, "nothing", action="confirm").status == "not_found"


@pytest.mark.integration
def test_sweep_all_queues_a_sweep_for_every_pending_proposal(conn: psycopg.Connection) -> None:
    _park(conn, "a")
    _park(conn, "b")
    _park(conn, "c")
    decide_current(conn, "c", action="cancel")  # already deciding: left alone

    queued = sweep_all(conn)

    assert len(queued) == 2
    assert _decisions(conn) == [
        ("c", 1, "cancel", "cli"),
        ("a", 1, "sweep", "sweep"),
        ("b", 1, "sweep", "sweep"),
    ]


@pytest.mark.integration
def test_list_shows_revision_status_and_the_dry_run_it_was_made_under(
    conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    _park(conn, "a", revision=2)
    _park(conn, "b")
    decide_current(conn, "b", action="confirm")

    count = list_pending(conn)

    out = capsys.readouterr().out
    assert count == 2
    assert "a  r2  pending  Design review" in out
    assert "b  r1  deciding  Design review" in out
    assert "dry_run when proposed: True" in out
    assert "Overlaps Standup" in out


# --- reconciliation on demand ------------------------------------------------------


def test_reconcile_on_demand_reports_what_it_did(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.channel.reconcile import ReconcileResult

    monkeypatch.setattr(
        approve,
        "reconcile",
        lambda session, **kwargs: ReconcileResult(recorded=2, closed=1, errors=0),
    )

    line = approve.reconcile_now(cast(GraphSession, object()))

    assert line == "Recorded 2 parked thread(s); closed 1 row(s); 0 error(s)."
