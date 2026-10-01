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

from app.channel import worker
from app.channel.decide import card_token, decide
from app.channel.park import ProposalRecord, record_park
from app.channel.worker import (
    ACT_INTERRUPTED,
    ATTEMPTS_EXHAUSTED,
    UNEXPECTED_REVISION,
    OpenDecision,
    Step,
    apply_open,
    settle_decided,
    settle_failed,
    step_for,
)
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


def test_a_revision_the_decision_cannot_explain_is_resynced_not_failed() -> None:
    """A parked thread is awaiting the owner. Failing it would bury a live
    proposal; showing its real revision again lets the owner decide."""
    view = _view(parked=True, revision=2, next_=("await_approval",))
    step = step_for(view, MessageStatus.AWAITING_APPROVAL, _decision("confirm"))
    assert (step.kind, step.outcome, step.reason) == ("settle", "resync", UNEXPECTED_REVISION)


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
    calendar_id: str = "test-calendar"
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


def _confirm(conn: psycopg.Connection, message_id: str = "m1", *, revision: int = 1) -> Any:
    """A Confirm from a current card: it carries the card's token (M17, D2)."""
    row = conn.execute(
        "SELECT args_hash, dry_run, generation FROM proposals WHERE message_id = %s",
        (message_id,),
    ).fetchone()
    assert row is not None and row[0] is not None
    return decide(
        conn,
        message_id,
        action="confirm",
        revision=revision,
        via="web",
        token=card_token(row[0], row[1], row[2]),
        dry_run=row[1],
    )


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
    _confirm(conn, "m1", revision=1)

    apply_open(session, announce=lambda record: announced.append(record.message_id))

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

    apply_open(session, announce=lambda record: announced.append(record.message_id))

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
    _confirm(conn, "m1", revision=1)
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
    result = _confirm(conn, "m1", revision=1)
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


# --- retries, the lease, and crashes (16.7) -----------------------------------


class Crash(BaseException):
    """A process dying: not caught by anything that handles `Exception`."""


@dataclass(slots=True)
class CountingSession(GraphSession):
    """Counts applications. A resume is the decision being applied."""

    resumes: int = 0
    redrives: int = 0
    fail_resumes: int = 0
    """Resumes that fail before reaching the graph, as a lost connection would."""

    def resume(self, message_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        self.resumes += 1
        if self.fail_resumes:
            self.fail_resumes -= 1
            raise RuntimeError("checkpointer unavailable")
        return GraphSession.resume(self, message_id, decision)

    def redrive(self, message_id: str) -> dict[str, Any]:
        self.redrives += 1
        return GraphSession.redrive(self, message_id)


def _counting(
    conn: psycopg.Connection, *, pipeline: FakePipeline | None = None, fail_resumes: int = 0
) -> CountingSession:
    deps = Deps(
        gmail=cast(Any, FakeGmail()),
        pipeline=cast(Any, pipeline or FakePipeline()),
        calendar=cast(Any, FakeCalendar()),
        ledger=MessageLedger(conn),
        user_timezone="Asia/Karachi",
        pipeline_version="0123456789ab",
    )
    return CountingSession(
        deps=deps,
        conn=conn,
        checkpointer=InMemorySaver(),
        trace=False,
        fail_resumes=fail_resumes,
    )


def _open(conn: psycopg.Connection) -> tuple[int, float, bool] | None:
    """The open decision's attempts, seconds until its next attempt, and lease."""
    row = conn.execute(
        """
        SELECT attempts, EXTRACT(EPOCH FROM next_attempt_at - now()), lease_until IS NOT NULL
          FROM decisions WHERE message_id = 'm1' AND outcome IS NULL
        """
    ).fetchone()
    return None if row is None else (row[0], float(row[1]), row[2])


def _make_due(conn: psycopg.Connection) -> None:
    conn.execute("UPDATE decisions SET next_attempt_at = now() WHERE message_id = 'm1'")


def _expire_lease(conn: psycopg.Connection) -> None:
    conn.execute(
        "UPDATE decisions SET lease_until = now() - interval '1 second' WHERE message_id = 'm1'"
    )


def _crash_once(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    original = getattr(worker, name)
    calls = {"n": 0}

    def crashing(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise Crash
        return original(*args, **kwargs)

    monkeypatch.setattr(worker, name, crashing)


@pytest.mark.integration
def test_failed_attempts_wait_one_then_ten_minutes_then_settle(conn: psycopg.Connection) -> None:
    session = _counting(conn, pipeline=FakePipeline(failing_calls={2, 3, 4}))
    _parked(conn, session)
    decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")

    assert apply_open(session) == [("m1", "retrying")]
    assert _open(conn) == (1, 60.0, False)
    assert apply_open(session) == []  # not due yet

    _make_due(conn)
    assert apply_open(session) == [("m1", "retrying")]
    assert _open(conn) == (2, 600.0, False)

    _make_due(conn)
    assert apply_open(session) == [("m1", "failed")]

    assert _open(conn) is None
    assert _outcomes(conn) == [("failed", ATTEMPTS_EXHAUSTED, True)]
    assert _proposal(conn) == ("failed", 1, None)
    assert _ledger(conn) is MessageStatus.FAILED
    # The edit was applied once; after that the thread was only re-driven.
    assert (session.resumes, session.redrives) == (1, 2)


@pytest.mark.integration
def test_a_retry_that_succeeds_still_lands_the_edit(conn: psycopg.Connection) -> None:
    session = _counting(conn, pipeline=FakePipeline(failing_calls={2}))
    _parked(conn, session)
    decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")
    apply_open(session)

    _make_due(conn)
    assert apply_open(session) == [("m1", "reparked")]

    assert _proposal(conn) == ("pending", 2, None)
    assert session.resumes == 1


@pytest.mark.integration
def test_a_decision_that_never_reaches_the_graph_returns_as_no_effect(
    conn: psycopg.Connection,
) -> None:
    """The owner's card comes back, rather than the proposal being lost."""
    session = _counting(conn, fail_resumes=3)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)

    apply_open(session)
    _make_due(conn)
    apply_open(session)
    _make_due(conn)
    assert apply_open(session) == [("m1", "no_effect")]

    assert _proposal(conn) == ("pending", 1, None)
    assert _outcomes(conn) == [("no_effect", ATTEMPTS_EXHAUSTED, True)]
    assert _ledger(conn) is MessageStatus.AWAITING_APPROVAL
    assert _confirm(conn, "m1", revision=1).status == "queued"


@pytest.mark.integration
def test_a_second_worker_skips_a_leased_decision(conn: psycopg.Connection) -> None:
    session = _counting(conn)
    _parked(conn, session)
    result = _confirm(conn, "m1", revision=1)
    assert result.decision_id is not None
    assert worker._take_lease(conn, result.decision_id)

    assert apply_open(session) == []

    assert session.resumes == 0
    assert _proposal(conn) == ("deciding", 1, None)


@pytest.mark.integration
def test_a_crash_after_taking_the_lease_converges_once_it_expires(
    conn: psycopg.Connection,
) -> None:
    session = _counting(conn)
    _parked(conn, session)
    result = _confirm(conn, "m1", revision=1)
    assert result.decision_id is not None
    assert worker._take_lease(conn, result.decision_id)  # the worker that died

    _expire_lease(conn)
    assert apply_open(session) == [("m1", "skipped")]

    assert session.resumes == 1


@pytest.mark.integration
def test_a_crash_after_the_resume_converges_without_resuming_again(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    _crash_once(monkeypatch, "_settle")

    with pytest.raises(Crash):
        apply_open(session)
    assert _ledger(conn) is MessageStatus.SKIPPED
    assert _proposal(conn) == ("deciding", 1, None)

    _expire_lease(conn)
    assert apply_open(session) == [("m1", "skipped")]

    assert session.resumes == 1
    assert _proposal(conn) == ("decided", 1, "skipped")


@pytest.mark.integration
def test_a_crash_inside_the_settle_leaves_nothing_half_written(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _counting(conn)
    _parked(conn, session)
    decide(conn, "m1", action="cancel", revision=1, via="web")
    _crash_once(monkeypatch, "_mark_decided")

    with pytest.raises(Crash):
        apply_open(session)
    # The decision was closed before the crash, and the close was rolled back
    # with the rest of the settle.
    assert _proposal(conn) == ("deciding", 1, None)
    assert _open(conn) is not None

    _expire_lease(conn)
    assert apply_open(session) == [("m1", "rejected")]

    assert session.resumes == 1
    assert _outcomes(conn) == [("rejected", None, True)]


@pytest.mark.integration
def test_a_crash_after_the_settle_applies_nothing_twice(conn: psycopg.Connection) -> None:
    pipeline = FakePipeline(extractions=[_meeting(), _meeting("Design review, 5pm")])
    session = _counting(conn, pipeline=pipeline)
    _parked(conn, session)
    decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")

    def dies(record: ProposalRecord) -> None:
        raise Crash

    with pytest.raises(Crash):
        apply_open(session, announce=dies)

    assert apply_open(session) == []
    assert session.resumes == 1
    assert _proposal(conn) == ("pending", 2, None)
    assert _outcomes(conn) == [("reparked", None, True)]


# --- fixes from the review of the queue (2026-10-01) ---------------------------


@pytest.mark.integration
def test_a_thread_moved_behind_the_workers_back_is_shown_again(conn: psycopg.Connection) -> None:
    """Something other than the worker edited the thread (the M15 CLI still
    can, until 16.10). The live proposal comes back at its real revision."""
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    GraphSession.resume(session, "m1", {"action": "edit", "correction": "make it 5pm"})
    announced: list[str] = []

    assert apply_open(session, announce=lambda record: announced.append(record.message_id)) == [
        ("m1", "no_effect")
    ]

    assert _proposal(conn) == ("pending", 2, None)
    assert _outcomes(conn) == [("no_effect", UNEXPECTED_REVISION, True)]
    assert _ledger(conn) is MessageStatus.AWAITING_APPROVAL
    assert announced == ["m1"]
    assert session.resumes == 0


@pytest.mark.integration
def test_one_broken_decision_does_not_hold_up_the_rest(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _counting(conn)
    for message_id in ("m1", "m2"):
        MessageLedger(conn).claim(message_id, message_id)
        session.start(message_id, message_id)
        pending = session.pending(message_id)
        assert pending is not None
        record_park(session, message_id, pending)
        decide(conn, message_id, action="cancel", revision=1, via="web")

    original = worker.apply_one

    def breaks_on_m1(session: Any, decision: OpenDecision, **kwargs: Any) -> str:
        if decision.message_id == "m1":
            raise RuntimeError("unexpected")
        return original(session, decision, **kwargs)

    monkeypatch.setattr(worker, "apply_one", breaks_on_m1)

    assert apply_open(session) == [("m1", "error"), ("m2", "rejected")]


@pytest.mark.integration
def test_giving_up_falls_back_to_failed_when_the_clean_settle_cannot_run(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _counting(conn)
    _parked(conn, session)
    decide(conn, "m1", action="cancel", revision=1, via="web")
    GraphSession.resume(session, "m1", {"action": "cancel"})
    conn.execute("UPDATE decisions SET attempts = 3 WHERE message_id = 'm1'")

    def cannot_settle(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("constraint surprise")

    monkeypatch.setattr(worker, "_settle", cannot_settle)

    assert apply_open(session) == [("m1", "failed")]
    assert _outcomes(conn) == [("failed", ATTEMPTS_EXHAUSTED, True)]
    # The ledger had already reached its final status, and keeps it.
    assert _ledger(conn) is MessageStatus.REJECTED


@pytest.mark.integration
def test_a_late_settle_never_touches_a_newer_decision(conn: psycopg.Connection) -> None:
    """Settles are scoped to their own decision, not to whatever is deciding."""
    pipeline = FakePipeline(extractions=[_meeting(), _meeting("Design review, 5pm")])
    session = _counting(conn, pipeline=pipeline)
    _parked(conn, session)
    first = decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")
    assert first.decision_id is not None
    apply_open(session)
    _confirm(conn, "m1", revision=2)

    # The first decision's worker wakes up late and settles it again.
    late = OpenDecision(
        id=first.decision_id,
        message_id="m1",
        revision=1,
        action="edit",
        correction="make it 5pm",
        attempts=0,
    )
    worker._settle(
        conn,
        late,
        Step("settle", outcome="rejected", final_status="rejected"),
        session.thread("m1"),
        MessageStatus.AWAITING_APPROVAL,
        None,
    )

    assert _proposal(conn) == ("deciding", 2, None)


@pytest.mark.integration
def test_settles_refuse_to_run_outside_a_transaction(migrated_database: str) -> None:
    """Outside one, the first write would commit on its own."""
    with psycopg.connect(migrated_database, autocommit=True) as bare:
        with pytest.raises(RuntimeError, match="transaction"):
            settle_failed(bare, 0, "nobody", reason="x")
        with pytest.raises(RuntimeError, match="transaction"):
            settle_decided(bare, 0, "nobody", final_status="skipped")


# --- what the decisions job asks before opening a session (16.9) ----------------


@pytest.mark.integration
def test_the_job_sees_the_oldest_open_decision_and_whether_it_is_due(
    conn: psycopg.Connection,
) -> None:
    from app.jobs.scheduler import decisions_status

    assert decisions_status(conn) == (None, False)

    session, _ = _world(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    decided_at = conn.execute("SELECT decided_at FROM decisions").fetchone()
    assert decided_at is not None
    assert decisions_status(conn) == (decided_at[0], True)

    conn.execute("UPDATE decisions SET next_attempt_at = now() + interval '1 minute'")
    assert decisions_status(conn) == (decided_at[0], False)  # waiting to retry

    conn.execute(
        "UPDATE decisions SET next_attempt_at = now(), lease_until = now() + interval '1 minute'"
    )
    assert decisions_status(conn) == (decided_at[0], False)  # another worker has it

    conn.execute("UPDATE decisions SET lease_until = NULL")
    apply_open(session)
    assert decisions_status(conn) == (None, False)
