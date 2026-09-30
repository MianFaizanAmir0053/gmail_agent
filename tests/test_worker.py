"""The worker (M16, D1): the only code that resumes a thread.

Each open decision moves one step at a time from what is stored -- the
thread's checkpoint and the ledger -- never from what the worker remembers.
The step table is tested as a pure function; the paths through a real graph
and real Postgres are tested end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.channel.decide import decide
from app.channel.park import record_park
from app.channel.worker import ACT_INTERRUPTED, OpenDecision, apply_open, settle_failed, step_for
from app.contracts import EmailMessage, ExtractionResult
from app.extraction.payloads import ClassifyPayload
from app.graph.nodes import Deps
from app.graph.runner import GraphSession, ThreadView
from app.store.ledger import MessageLedger, MessageStatus

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
START = datetime(2026, 10, 2, 11, 0, tzinfo=UTC)


def _decision(action: str = "confirm", revision: int = 1) -> OpenDecision:
    return OpenDecision(
        id=1, message_id="m1", revision=revision, action=action, correction=None, attempts=0
    )


def _view(*, parked: bool = False, revision: int = 1, next_: tuple[str, ...] = ()) -> ThreadView:
    return ThreadView(payload={"proposed": {}} if parked else None, revision=revision, next=next_)


# --- the step table (pure) ---------------------------------------------------


def test_a_thread_parked_at_the_decisions_revision_is_resumed() -> None:
    step = step_for(
        _view(parked=True, next_=("await_approval",)), MessageStatus.AWAITING_APPROVAL, _decision()
    )
    assert step.kind == "resume"


def test_a_thread_parked_one_revision_on_after_an_edit_is_settled_as_reparked() -> None:
    view = _view(parked=True, revision=2, next_=("await_approval",))
    step = step_for(view, MessageStatus.AWAITING_APPROVAL, _decision("edit"))
    assert (step.kind, step.outcome) == ("settle", "reparked")


def test_a_revision_the_decision_cannot_explain_fails_rather_than_guessing() -> None:
    view = _view(parked=True, revision=2, next_=("await_approval",))
    step = step_for(view, MessageStatus.AWAITING_APPROVAL, _decision("confirm"))
    assert (step.kind, step.outcome) == ("settle", "failed")


@pytest.mark.parametrize(
    "status", [MessageStatus.CREATED, MessageStatus.SKIPPED, MessageStatus.REJECTED]
)
def test_a_final_ledger_status_is_the_outcome(status: MessageStatus) -> None:
    step = step_for(_view(), status, _decision())
    assert (step.kind, step.outcome, step.final_status) == ("settle", status.value, status.value)


@pytest.mark.parametrize("next_", [("extract",), ("review",), ("conflicts",), ("reject",)])
def test_a_thread_stopped_mid_graph_is_re_driven(next_: tuple[str, ...]) -> None:
    step = step_for(_view(next_=next_), MessageStatus.AWAITING_APPROVAL, _decision("edit"))
    assert step.kind == "redrive"


def test_a_thread_stopped_before_act_is_never_re_driven() -> None:
    """Creating an event is not idempotent: a second run could book twice."""
    step = step_for(_view(next_=("act",)), MessageStatus.AWAITING_APPROVAL, _decision())
    assert (step.kind, step.outcome, step.reason) == ("settle", "failed", ACT_INTERRUPTED)


def test_a_thread_that_ended_without_an_outcome_fails() -> None:
    step = step_for(_view(), MessageStatus.AWAITING_APPROVAL, _decision())
    assert (step.kind, step.outcome) == ("settle", "failed")


# --- through a real graph and Postgres ----------------------------------------

EMAIL = EmailMessage(
    id="m1",
    thread_id="m1",
    subject="Design review",
    body_text="Thursday 4pm",
    sender="sara@example.com",
    recipients=["me@example.com"],
    received_at=NOW,
)


def _meeting(title: str = "Design review") -> ExtractionResult:
    return ExtractionResult(
        is_meeting=True,
        title=title,
        start_utc=START,
        end_utc=START + timedelta(hours=1),
        timezone="Asia/Karachi",
        attendees=["sara@example.com"],
        confidence=0.9,
        reasoning="",
    )


class FakeGmail:
    def get_message(self, message_id: str) -> EmailMessage:
        return EMAIL


@dataclass
class FakePipeline:
    extractions: list[ExtractionResult] = field(default_factory=list)
    failing_calls: set[int] = field(default_factory=set)
    calls: int = 0

    def classify(self, email: EmailMessage, **kwargs: Any) -> ClassifyPayload:
        return ClassifyPayload(is_meeting=True, confidence=0.9, reasoning="explicit time")

    def extract(self, email: EmailMessage, **kwargs: Any) -> ExtractionResult:
        self.calls += 1
        if self.calls in self.failing_calls:
            raise RuntimeError("model unavailable")
        return self.extractions.pop(0) if self.extractions else _meeting()


@dataclass
class FakeCalendar:
    dry_run: bool = True
    created: list[Any] = field(default_factory=list)

    def freebusy(self, start: datetime, end: datetime) -> list[Any]:
        return []

    def create_event(self, **kwargs: Any) -> str | None:
        if self.dry_run:
            return None
        self.created.append(kwargs)
        return "evt_1"


@dataclass
class BrokenLedger:
    """The graph's ledger, failing its final mark: the thread stops at `act`."""

    real: MessageLedger
    marks: int = 0

    def mark(self, *args: Any, **kwargs: Any) -> None:
        self.marks += 1
        raise RuntimeError("database went away")


def _world(
    conn: psycopg.Connection, *, pipeline: FakePipeline | None = None, ledger: Any = None
) -> tuple[GraphSession, list[str]]:
    deps = Deps(
        gmail=cast(Any, FakeGmail()),
        pipeline=cast(Any, pipeline or FakePipeline()),
        calendar=cast(Any, FakeCalendar()),
        ledger=ledger or MessageLedger(conn),
        user_timezone="Asia/Karachi",
        pipeline_version="0123456789ab",
    )
    session = GraphSession(deps=deps, conn=conn, checkpointer=InMemorySaver(), trace=False)
    announced: list[str] = []
    return session, announced


def _parked(conn: psycopg.Connection, session: GraphSession) -> None:
    MessageLedger(conn).claim("m1", "m1")
    session.start("m1", "m1")
    pending = session.pending("m1")
    assert pending is not None
    record_park(session, "m1", pending)


def _proposal(conn: psycopg.Connection) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT status, revision, final_status FROM proposals WHERE message_id = 'm1'"
    ).fetchone()
    assert row is not None
    return tuple(row)


def _outcomes(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT outcome, reason, settled_at IS NOT NULL FROM decisions "
        "WHERE message_id = 'm1' ORDER BY id"
    ).fetchall()


def _ledger(conn: psycopg.Connection) -> MessageStatus:
    entry = MessageLedger(conn).get("m1")
    assert entry is not None
    return entry.status


@pytest.mark.integration
def test_a_confirm_in_dry_run_settles_as_skipped(conn: psycopg.Connection) -> None:
    session, announced = _world(conn)
    _parked(conn, session)
    decide(conn, "m1", action="confirm", revision=1, via="web")

    apply_open(session, announce=lambda mid, _: announced.append(mid))

    assert _ledger(conn) is MessageStatus.SKIPPED
    assert _proposal(conn) == ("decided", 1, "skipped")
    assert _outcomes(conn) == [("skipped", None, True)]


@pytest.mark.integration
def test_a_cancel_settles_as_rejected(conn: psycopg.Connection) -> None:
    session, _ = _world(conn)
    _parked(conn, session)
    decide(conn, "m1", action="cancel", revision=1, via="web")

    apply_open(session)

    assert _ledger(conn) is MessageStatus.REJECTED
    assert _proposal(conn) == ("decided", 1, "rejected")
    assert _outcomes(conn) == [("rejected", None, True)]


@pytest.mark.integration
def test_an_edit_reparks_at_the_next_revision_and_is_announced(conn: psycopg.Connection) -> None:
    pipeline = FakePipeline(extractions=[_meeting(), _meeting("Design review, 5pm")])
    session, announced = _world(conn, pipeline=pipeline)
    _parked(conn, session)
    decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")

    apply_open(session, announce=lambda mid, _: announced.append(mid))

    assert _proposal(conn) == ("pending", 2, None)
    assert _outcomes(conn) == [("reparked", None, True)]
    assert _ledger(conn) is MessageStatus.AWAITING_APPROVAL
    assert announced == ["m1"]
    payload = conn.execute("SELECT payload FROM proposals WHERE message_id = 'm1'").fetchone()
    assert payload is not None
    assert payload[0]["title"] == "Design review, 5pm"


@pytest.mark.integration
def test_a_thread_stopped_mid_graph_is_re_driven_to_a_repark(conn: psycopg.Connection) -> None:
    pipeline = FakePipeline(failing_calls={2})
    session, _ = _world(conn, pipeline=pipeline)
    _parked(conn, session)
    decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")
    # The decision is consumed and extraction fails: the state a crash or an
    # outage leaves behind.
    with pytest.raises(RuntimeError):
        session.resume("m1", {"action": "edit", "correction": "make it 5pm"})

    apply_open(session)

    assert _proposal(conn) == ("pending", 2, None)
    assert _outcomes(conn) == [("reparked", None, True)]


@pytest.mark.integration
def test_a_thread_stopped_before_act_fails_and_is_never_re_driven(
    conn: psycopg.Connection,
) -> None:
    broken = BrokenLedger(MessageLedger(conn))
    session, _ = _world(conn, ledger=broken)
    MessageLedger(conn).claim("m1", "m1")
    session.start("m1", "m1")
    pending = session.pending("m1")
    assert pending is not None
    record_park(session, "m1", pending)
    decide(conn, "m1", action="confirm", revision=1, via="web")
    with pytest.raises(RuntimeError):
        session.resume("m1", {"action": "confirm"})
    marks_before = broken.marks

    apply_open(session)

    assert broken.marks == marks_before
    assert _proposal(conn) == ("failed", 1, None)
    assert _outcomes(conn) == [("failed", ACT_INTERRUPTED, True)]
    assert _ledger(conn) is MessageStatus.FAILED


@pytest.mark.integration
def test_a_late_failed_settle_never_overwrites_a_final_ledger_status(
    conn: psycopg.Connection,
) -> None:
    session, _ = _world(conn)
    _parked(conn, session)
    result = decide(conn, "m1", action="confirm", revision=1, via="web")
    assert result.decision_id is not None
    MessageLedger(conn).mark("m1", MessageStatus.CREATED, calendar_event_id="evt_1")

    with conn.transaction():
        settle_failed(conn, result.decision_id, "m1", reason="late")

    entry = MessageLedger(conn).get("m1")
    assert entry is not None
    assert entry.status is MessageStatus.CREATED
    assert entry.calendar_event_id == "evt_1"


@pytest.mark.integration
def test_an_outcome_is_written_only_once(conn: psycopg.Connection) -> None:
    session, _ = _world(conn)
    _parked(conn, session)
    decide(conn, "m1", action="cancel", revision=1, via="web")
    apply_open(session)
    decision_id = conn.execute("SELECT id FROM decisions WHERE message_id = 'm1'").fetchone()
    assert decision_id is not None

    with conn.transaction():
        settle_failed(conn, decision_id[0], "m1", reason="late")

    assert _outcomes(conn) == [("rejected", None, True)]
    assert _proposal(conn) == ("decided", 1, "rejected")
