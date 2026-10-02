"""The worker (M16, D1): the only code that resumes a thread.

Each open decision moves one step at a time from what is stored -- the
thread's checkpoint and the ledger -- never from what the worker remembers.
The step table is tested as a pure function; the paths through a real graph
and real Postgres are tested end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.channel import worker
from app.channel.decide import card_token, decide, request_withdraw
from app.channel.park import ProposalRecord, record_park
from app.channel.worker import (
    ACT_INTERRUPTED,
    ATTEMPTS_EXHAUSTED,
    NO_OUTCOME,
    UNEXPECTED_REVISION,
    OpenDecision,
    Step,
    apply_open,
    settle_decided,
    settle_failed,
    step_for,
    unconfirmed_writes,
)
from app.contracts import EmailMessage, ExtractionResult
from app.extraction.payloads import ClassifyPayload
from app.graph.nodes import Deps
from app.graph.runner import GraphSession, ThreadView
from app.policy import audit, control
from app.policy.contacts import unconfirmed_outsiders
from app.policy.hashing import args_key
from app.policy.registry import Registry
from app.store.ledger import MessageLedger, MessageStatus

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
START = datetime(2026, 10, 2, 11, 0, tzinfo=UTC)
KEY = args_key("test-key")


def _decision(
    action: str = "confirm", revision: int = 1, *, action_status: str | None = None
) -> OpenDecision:
    return OpenDecision(
        id=1,
        message_id="m1",
        revision=revision,
        action=action,
        correction=None,
        attempts=0,
        action_id=None if action_status is None else 1,
        nonce=None if action_status is None else "0" * 32,
        action_status=action_status,
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


def test_a_thread_stopped_before_act_with_nothing_to_finish_from_is_never_re_driven() -> None:
    """A Confirm from before M17 has no approval: its write could not be
    found again, so a second run could book twice."""
    step = step_for(_view(next_=("act",)), MessageStatus.AWAITING_APPROVAL, _decision())
    assert (step.kind, step.outcome, step.reason) == ("settle", "failed", ACT_INTERRUPTED)


@pytest.mark.parametrize(
    "status", ["approved", "executing", "done", "dry_run", "refused", "failed"]
)
def test_a_thread_stopped_before_act_with_an_approval_is_re_driven(status: str) -> None:
    """The registry checks a new action, finishes a begun one from what it
    stored, and returns a settled one's outcome with its reason (M17, D3)."""
    step = step_for(
        _view(next_=("act",)), MessageStatus.AWAITING_APPROVAL, _decision(action_status=status)
    )
    assert step.kind == "redrive"


def test_a_thread_that_ended_without_an_outcome_fails() -> None:
    step = step_for(_view(), MessageStatus.AWAITING_APPROVAL, _decision())
    assert (step.kind, step.outcome) == ("settle", "failed")


def test_a_refusal_settles_with_the_registrys_reason() -> None:
    """`act` marked the message FAILED with a fixed phrase; the decision says
    the same, not "no outcome recorded"."""
    step = step_for(
        _view(), MessageStatus.FAILED, _decision(), ledger_error="made under another mode"
    )
    assert (step.kind, step.outcome, step.reason) == ("settle", "failed", "made under another mode")


def test_a_failure_in_words_of_its_own_is_not_copied_into_the_decision() -> None:
    """Only the registry's fixed phrases: anything else may quote a model."""
    step = step_for(_view(), MessageStatus.FAILED, _decision(), ledger_error="ValueError: Sara's")
    assert step.reason == NO_OUTCOME


# --- through a real graph and Postgres ----------------------------------------

EMAIL = EmailMessage(
    id="m1",
    thread_id="t1",  # not the message id: the ledger's thread id is (M17, D4)
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
    """The email, and its thread as the recipient rule reads it: by default
    the owner wrote to Sara, so she is a participant (M17, D4)."""

    def __init__(self) -> None:
        self.thread: dict[str, Any] = {
            "messages": [
                {
                    "labelIds": ["SENT"],
                    "payload": {"headers": [{"name": "To", "value": "sara@example.com"}]},
                }
            ]
        }
        self.down = False
        self.down_after: int | None = None
        """Thread reads that succeed before Gmail goes down."""
        self.reads = 0

    def get_message(self, message_id: str) -> EmailMessage:
        return EMAIL

    def message_metadata(self, message_id: str) -> Any:
        if self.down:
            raise ConnectionError("Gmail unavailable")
        return SimpleNamespace(thread_id=EMAIL.thread_id)

    def thread_headers(self, thread_id: str) -> dict[str, Any]:
        self.reads += 1
        if self.down or (self.down_after is not None and self.reads > self.down_after):
            raise ConnectionError("Gmail unavailable")
        # Only the email's own thread has Sara in it: reading any other
        # thread -- the message id, say -- finds no one.
        return self.thread if thread_id == EMAIL.thread_id else {"messages": []}


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
    """The calendar the registry writes to, keeping events by calendar and id.
    Each `fail_*` count fails that many calls: before Google makes the event,
    after it does, or when asked for one."""

    dry_run: bool = True
    calendar_id: str = "test-calendar"
    events: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    calls: int = 0
    """Every insert asked for, made or not: a blind second insert shows here."""
    inserts: int = 0
    fail_before: int = 0
    fail_after: int = 0
    fail_find: int = 0

    def freebusy(self, start: datetime, end: datetime) -> list[Any]:
        return []

    def insert(self, calendar_id: str, body: dict[str, Any], *, event_id: str) -> str | None:
        self.calls += 1
        if self.dry_run:
            return None
        if self.fail_before:
            self.fail_before -= 1
            raise ConnectionError("network down before the insert")
        if (calendar_id, event_id) not in self.events:
            self.inserts += 1
            self.events[(calendar_id, event_id)] = body
        if self.fail_after:
            self.fail_after -= 1
            raise ConnectionError("network down after Google made the event")
        return event_id

    def find(self, calendar_id: str, event_id: str) -> str | None:
        if self.fail_find:
            self.fail_find -= 1
            raise ConnectionError("Google unavailable")
        return event_id if (calendar_id, event_id) in self.events else None


@dataclass
class BrokenLedger:
    """The graph's ledger, failing its marks: the thread stops at `act`.
    `failures` fails only the first that many; None fails every one."""

    real: MessageLedger
    failures: int | None = None
    marks: int = 0

    def mark(self, *args: Any, **kwargs: Any) -> None:
        self.marks += 1
        if self.failures is None:
            raise RuntimeError("database went away")
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("database went away")
        self.real.mark(*args, **kwargs)


def _deps(
    conn: psycopg.Connection,
    *,
    pipeline: FakePipeline | None = None,
    ledger: Any = None,
    calendar: FakeCalendar | None = None,
) -> Deps:
    calendar = calendar or FakeCalendar()
    gmail = FakeGmail()
    return Deps(
        gmail=cast(Any, gmail),
        pipeline=cast(Any, pipeline or FakePipeline()),
        calendar=cast(Any, calendar),
        ledger=ledger or MessageLedger(conn),
        user_timezone="Asia/Karachi",
        registry=Registry(
            conn, calendar, key=KEY, outsiders=partial(unconfirmed_outsiders, conn, gmail)
        ),
        pipeline_version="0123456789ab",
        args_key=KEY,
    )


def _world(
    conn: psycopg.Connection,
    *,
    pipeline: FakePipeline | None = None,
    ledger: Any = None,
    calendar: FakeCalendar | None = None,
) -> tuple[GraphSession, list[str]]:
    deps = _deps(conn, pipeline=pipeline, ledger=ledger, calendar=calendar)
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
def test_a_confirm_from_before_m17_stopped_at_act_fails_and_is_never_re_driven(
    conn: psycopg.Connection,
) -> None:
    """It has no approval, so nothing could find its write again."""
    broken = BrokenLedger(MessageLedger(conn))
    session, _ = _world(conn, ledger=broken)
    MessageLedger(conn).claim("m1", "m1")
    session.start("m1", "m1")
    pending = session.pending("m1")
    assert pending is not None
    record_park(session, "m1", pending)
    _confirm(conn, "m1", revision=1)
    conn.execute("DELETE FROM outbound_actions WHERE message_id = 'm1'")  # as M16 left it
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
    conn: psycopg.Connection,
    *,
    pipeline: FakePipeline | None = None,
    fail_resumes: int = 0,
    calendar: FakeCalendar | None = None,
    ledger: Any = None,
) -> CountingSession:
    return CountingSession(
        deps=_deps(conn, pipeline=pipeline, calendar=calendar, ledger=ledger),
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
    old = _token(conn)
    _confirm(conn, "m1", revision=1)

    apply_open(session)
    _make_due(conn)
    apply_open(session)
    _make_due(conn)
    assert apply_open(session) == [("m1", "no_effect")]

    assert _proposal(conn) == ("pending", 1, None)
    assert _outcomes(conn) == [("no_effect", ATTEMPTS_EXHAUSTED, True)]
    assert _ledger(conn) is MessageStatus.AWAITING_APPROVAL
    # No approval outlives its decision (M17, D2), and the card comes back
    # under a new generation: the same hash, yet the old card's Confirm dies.
    assert _action(conn) == ("refused", "attempts exhausted", False)
    stale = decide(conn, "m1", action="confirm", revision=1, via="web", token=old, dry_run=True)
    assert stale.status == "stale"
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
    assert _action(conn) == ("refused", "the proposal changed", False)


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


def _now(conn: psycopg.Connection) -> Any:
    """The transaction's `now()`, which every statement in a test shares."""
    row = conn.execute("SELECT now()").fetchone()
    assert row is not None
    return row[0]


@pytest.mark.integration
def test_the_job_sees_when_a_decision_became_due_and_whether_it_is_due(
    conn: psycopg.Connection,
) -> None:
    """The stuck clock runs from when a decision became due, so one waiting
    for its next attempt, or leased, or held, is not stuck."""
    from app.jobs.scheduler import decisions_status

    conn.execute("UPDATE control SET paused = false")
    assert decisions_status(conn) == (None, False, False)

    session, _ = _world(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    now = _now(conn)
    assert decisions_status(conn) == (now, True, False)

    conn.execute("UPDATE decisions SET next_attempt_at = now() + interval '1 minute'")
    status = decisions_status(conn)
    assert status.due is False  # waiting to retry
    assert status.oldest is not None and status.oldest > now  # not due, so not stuck

    conn.execute(
        "UPDATE decisions SET next_attempt_at = now(), lease_until = now() + interval '1 minute'"
    )
    assert decisions_status(conn) == (None, False, False)  # another worker has it

    conn.execute("UPDATE decisions SET lease_until = NULL")
    control.switch(conn, paused=True, via="cli")
    assert decisions_status(conn) == (None, False, True)  # paused: held (M17, D6)

    # Held for two hours, then resumed: due again, its clock starting afresh.
    conn.execute("UPDATE decisions SET next_attempt_at = now() - interval '2 hours'")
    control.switch(conn, paused=False, via="cli")
    assert decisions_status(conn) == (now, True, False)

    apply_open(session)
    assert decisions_status(conn) == (None, False, False)


@pytest.mark.integration
def test_an_edit_held_by_the_cap_is_pushed_back_not_left_due(conn: psycopg.Connection) -> None:
    """Neither a session every fifteen seconds, nor stuck, nor ahead of a
    Confirm that could still apply (D5)."""
    from app.jobs.scheduler import decisions_status

    conn.execute("UPDATE control SET paused = false")
    session = _counting(conn)
    _parked(conn, session)
    decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")
    session.gate = cast(Any, CapGate(room=False))

    assert apply_open(session) == [("m1", "waiting")]

    status = decisions_status(conn)
    assert status.due is False
    assert status.oldest is not None and status.oldest > _now(conn)


# --- writes that can be finished (M17, 17.5-17.6) -------------------------------


def _action(conn: psycopg.Connection) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT status, reason, request IS NOT NULL FROM outbound_actions WHERE message_id = 'm1'"
    ).fetchone()
    assert row is not None
    return tuple(row)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("fail_before", "fail_after", "ledger_failures", "calls"),
    [
        (1, 0, 0, 2),  # before Google made the event: sent again, once
        (0, 1, 0, 1),  # after Google made it: found, never sent again
        (0, 0, 1, 1),  # after the action was done: its outcome stands
    ],
    ids=["before-insert", "after-insert", "after-done"],
)
def test_a_live_write_cut_off_anywhere_ends_with_exactly_one_event(
    conn: psycopg.Connection, fail_before: int, fail_after: int, ledger_failures: int, calls: int
) -> None:
    """The 17.6 exit: one event, the ledger CREATED, no action left executing."""
    calendar = FakeCalendar(dry_run=False, fail_before=fail_before, fail_after=fail_after)
    ledger = BrokenLedger(MessageLedger(conn), failures=ledger_failures)
    session = _counting(conn, calendar=calendar, ledger=ledger)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)

    assert apply_open(session) == [("m1", "retrying")]
    _make_due(conn)
    assert apply_open(session) == [("m1", "created")]

    [(calendar_id, event_id)] = calendar.events
    assert calendar_id == "test-calendar" and calendar.calls == calls
    entry = MessageLedger(conn).get("m1")
    assert entry is not None
    assert (entry.status, entry.calendar_event_id) == (MessageStatus.CREATED, event_id)
    assert _proposal(conn) == ("decided", 1, "created")
    assert _action(conn) == ("done", None, False)
    # The decision was applied once; the retry only re-drove `act`.
    assert (session.resumes, session.redrives) == (1, 1)


@pytest.mark.integration
def test_a_dry_run_cut_off_at_the_ledger_settles_from_what_was_stored(
    conn: psycopg.Connection,
) -> None:
    calendar = FakeCalendar()
    session = _counting(conn, calendar=calendar, ledger=BrokenLedger(MessageLedger(conn), 1))
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)

    assert apply_open(session) == [("m1", "retrying")]
    _make_due(conn)
    assert apply_open(session) == [("m1", "skipped")]

    assert calendar.events == {}
    assert _action(conn) == ("dry_run", None, False)
    assert _ledger(conn) is MessageStatus.SKIPPED


def _cut_off_and_exhausted(conn: psycopg.Connection, calendar: FakeCalendar) -> CountingSession:
    """A live Confirm whose write was cut off, with its attempts spent."""
    session = _counting(conn, calendar=calendar)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    assert apply_open(session) == [("m1", "retrying")]
    assert _action(conn)[0] == "executing"
    conn.execute("UPDATE decisions SET attempts = 3, next_attempt_at = now()")
    return session


@pytest.mark.integration
def test_giving_up_on_a_write_google_made_settles_it_as_created(
    conn: psycopg.Connection,
) -> None:
    calendar = FakeCalendar(dry_run=False, fail_after=1)
    session = _cut_off_and_exhausted(conn, calendar)

    assert apply_open(session) == [("m1", "created")]

    [(_, event_id)] = calendar.events
    entry = MessageLedger(conn).get("m1")
    assert entry is not None
    assert (entry.status, entry.calendar_event_id) == (MessageStatus.CREATED, event_id)
    assert _outcomes(conn) == [("created", None, True)]
    assert _proposal(conn) == ("decided", 1, "created")
    assert _action(conn) == ("done", None, False)
    assert calendar.inserts == 1


@pytest.mark.integration
def test_giving_up_on_a_write_google_never_made_fails_it(conn: psycopg.Connection) -> None:
    calendar = FakeCalendar(dry_run=False, fail_before=1)
    session = _cut_off_and_exhausted(conn, calendar)

    assert apply_open(session) == [("m1", "failed")]

    assert calendar.events == {}
    assert _outcomes(conn) == [("failed", ATTEMPTS_EXHAUSTED, True)]
    assert _ledger(conn) is MessageStatus.FAILED
    assert _action(conn) == ("failed", audit.REASONS["exhausted"], False)


@pytest.mark.integration
def test_giving_up_when_google_cannot_be_asked_settles_nothing(conn: psycopg.Connection) -> None:
    """Usually why the attempts ran out. It asks again in an hour."""
    calendar = FakeCalendar(dry_run=False, fail_before=1, fail_find=2)
    session = _cut_off_and_exhausted(conn, calendar)

    assert apply_open(session) == [("m1", "unconfirmed")]
    attempts, wait, leased = cast(tuple[int, float, bool], _open(conn))
    assert (attempts, leased) == (3, False) and 3590 < wait <= 3600
    _make_due(conn)
    assert apply_open(session) == [("m1", "unconfirmed")]

    assert _action(conn)[0] == "executing"
    assert _ledger(conn) is MessageStatus.AWAITING_APPROVAL
    unconfirmed = conn.execute(
        "SELECT count(*) FROM audit_log WHERE kind = 'write_unconfirmed' AND message_id = 'm1'"
    ).fetchone()
    assert unconfirmed == (1,)  # audited once, not every hour


@dataclass
class AlertChannels:
    names: frozenset[str] = frozenset({"web_push"})
    asked: list[str] = field(default_factory=list)

    def alert(self, code: str, *, skip: frozenset[str] = frozenset()) -> set[str]:
        self.asked.append(code)
        return set(self.names - skip)


@pytest.mark.integration
def test_a_write_that_could_not_be_confirmed_is_counted_apart_and_alerted_once(
    conn: psycopg.Connection,
) -> None:
    """It asks Google again every hour: held, not stuck (D3). The owner hears
    of it once (17.11)."""
    from app.config import Settings
    from app.jobs.scheduler import decisions_status
    from app.jobs.watch import watch

    conn.execute("UPDATE control SET paused = false, budget_state = 'ok'")
    conn.execute("DELETE FROM model_spend")
    conn.execute("DELETE FROM alerts_sent")
    calendar = FakeCalendar(dry_run=False, fail_before=1, fail_find=1)
    session = _cut_off_and_exhausted(conn, calendar)
    assert apply_open(session) == [("m1", "unconfirmed")]

    [decision_id] = unconfirmed_writes(conn)
    status = decisions_status(conn)
    assert status.due is False  # it asks Google again in an hour
    assert status.oldest is not None and status.oldest > _now(conn)  # so it is not stuck

    settings = Settings(
        _env_file=None, database_url="postgresql://localhost/test", gemini_api_key="k"
    )
    channels = AlertChannels()
    watched = watch(conn, settings, channels)
    watch(conn, settings, channels)

    assert watched.unconfirmed == 1
    assert channels.asked == ["write_unconfirmed"]
    sent = conn.execute("SELECT subject FROM alerts_sent WHERE code = 'write_unconfirmed'")
    assert sent.fetchall() == [(str(decision_id),)]


@pytest.mark.integration
def test_while_paused_nothing_is_applied(conn: psycopg.Connection) -> None:
    """The Confirm stays parked, so a Withdraw could still reach it (D6). It
    applies as soon as the owner resumes."""
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    conn.execute("UPDATE control SET paused = true")

    assert apply_open(session) == []
    assert session.resumes == 0
    assert _proposal(conn) == ("deciding", 1, None)

    conn.execute("UPDATE control SET paused = false")
    assert apply_open(session) == [("m1", "skipped")]
    assert (session.resumes, session.redrives) == (1, 0)


@pytest.mark.integration
def test_a_pause_that_lands_mid_apply_holds_the_write_without_spending_attempts(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paused after the worker looked: the registry stops the action itself,
    and the worker releases it as due, costing no attempt."""
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    conn.execute("UPDATE control SET paused = true")
    monkeypatch.setattr(worker, "control", SimpleNamespace(is_paused=lambda conn: False))

    assert apply_open(session) == [("m1", "paused")]
    attempts, wait, leased = cast(tuple[int, float, bool], _open(conn))
    assert (attempts, leased) == (0, False) and wait <= 0
    assert _action(conn)[0] == "approved"

    conn.execute("UPDATE control SET paused = false")
    assert apply_open(session) == [("m1", "skipped")]
    assert (session.resumes, session.redrives) == (1, 1)


@pytest.mark.integration
def test_a_last_write_that_failed_is_looked_up_later_not_at_once(
    conn: psycopg.Connection,
) -> None:
    """Google can finish an insert after the call timed out on this side."""
    calendar = FakeCalendar(dry_run=False, fail_before=2)
    session = _counting(conn, calendar=calendar)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    assert apply_open(session) == [("m1", "retrying")]
    conn.execute("UPDATE decisions SET attempts = 2, next_attempt_at = now()")

    assert apply_open(session) == [("m1", "retrying")]  # the third write failed

    attempts, wait, leased = cast(tuple[int, float, bool], _open(conn))
    assert (attempts, leased) == (3, False) and 590 < wait <= 600
    assert _action(conn)[0] == "executing"
    _make_due(conn)
    assert apply_open(session) == [("m1", "failed")]  # looked up: never made


@pytest.mark.integration
def test_giving_up_never_fails_a_write_that_may_exist(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when the clean settle cannot run, a begun write is not failed on
    a guess (D3): the decision stays open and is asked about again."""
    session = _cut_off_and_exhausted(conn, FakeCalendar(dry_run=False, fail_before=1))

    def unreadable(*args: Any, **kwargs: Any) -> Step:
        raise RuntimeError("the checkpoint cannot be read")

    monkeypatch.setattr(worker, "step_for", unreadable)

    assert apply_open(session) == [("m1", "unconfirmed")]
    assert _action(conn)[0] == "executing"
    assert _ledger(conn) is MessageStatus.AWAITING_APPROVAL
    assert _open(conn) is not None


@pytest.mark.integration
def test_giving_up_on_a_dry_run_settles_it_as_skipped(conn: psycopg.Connection) -> None:
    """The registry ran it; only the ledger's mark kept failing."""
    session = _counting(conn, ledger=BrokenLedger(MessageLedger(conn)))
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    assert apply_open(session) == [("m1", "retrying")]
    _make_due(conn)
    assert apply_open(session) == [("m1", "retrying")]
    _make_due(conn)

    assert apply_open(session) == [("m1", "skipped")]

    entry = MessageLedger(conn).get("m1")
    assert entry is not None
    assert (entry.status, entry.error) == (MessageStatus.SKIPPED, "dry_run")
    assert _outcomes(conn) == [("skipped", None, True)]
    assert _proposal(conn) == ("decided", 1, "skipped")


@pytest.mark.integration
def test_a_mode_change_after_the_resume_fails_visibly_and_keeps_its_reason(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3's "a mode change after a resume": resumed, then held at `act` by a
    Pause, then a restart with DRY_RUN off. The registry refuses it; the
    ledger's mark is cut off once; the re-drive returns the stored refusal,
    and the decision gives its reason."""
    calendar = FakeCalendar()  # parked and approved under dry run
    session = _counting(conn, calendar=calendar, ledger=BrokenLedger(MessageLedger(conn), 1))
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    conn.execute("UPDATE control SET paused = true")
    with monkeypatch.context() as patched:
        patched.setattr(worker, "control", SimpleNamespace(is_paused=lambda conn: False))
        assert apply_open(session) == [("m1", "paused")]  # held at `act`
    conn.execute("UPDATE control SET paused = false")
    calendar.dry_run = False  # the restart

    assert apply_open(session) == [("m1", "retrying")]
    _make_due(conn)
    assert apply_open(session) == [("m1", "failed")]

    assert calendar.calls == 0
    assert _outcomes(conn) == [("failed", audit.REASONS["mode"], True)]
    assert _action(conn) == ("refused", audit.REASONS["mode"], False)


# --- checks before a Confirm (M17, 17.7) --------------------------------------------


def _token(conn: psycopg.Connection) -> str:
    row = conn.execute(
        "SELECT args_hash, dry_run, generation FROM proposals WHERE message_id = 'm1'"
    ).fetchone()
    assert row is not None
    return card_token(row[0], row[1], row[2])


@pytest.mark.integration
def test_a_confirm_approved_under_dry_run_and_applied_live_expires_the_proposal(
    conn: psycopg.Connection,
) -> None:
    """The owner's end test, in miniature: approved under dry run, then a
    restart with DRY_RUN off. Nothing runs, the proposal is expired, and the
    old card is refused as stale."""
    calendar = FakeCalendar()
    session = _counting(conn, calendar=calendar)
    _parked(conn, session)
    old = _token(conn)
    _confirm(conn, "m1", revision=1)
    calendar.dry_run = False
    announced: list[str] = []

    assert apply_open(session, announce=lambda r: announced.append(r.message_id)) == [
        ("m1", "no_effect")
    ]
    assert session.resumes == 0
    assert announced == []  # expired, never shown again
    assert _action(conn) == ("refused", audit.REASONS["mode"], False)

    assert apply_open(session) == [("m1", "rejected")]  # the expiry's sweep

    entry = MessageLedger(conn).get("m1")
    assert entry is not None
    assert (entry.status, entry.error) == (MessageStatus.REJECTED, "made under another mode")
    assert _outcomes(conn) == [
        ("no_effect", "made under another mode", True),
        ("rejected", "made under another mode", True),
    ]
    assert calendar.calls == 0
    stale = decide(conn, "m1", action="confirm", revision=1, via="web", token=old, dry_run=False)
    assert stale.status == "stale"


@pytest.mark.integration
@pytest.mark.parametrize("change", ["calendar", "canonical form"])
def test_a_confirm_whose_arguments_changed_returns_to_the_owner(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A deploy moved the calendar, or changed the canonical form, so the
    approval no longer matches what would run. The proposal comes back as it
    is now, under a new generation."""
    calendar = FakeCalendar()
    session = _counting(conn, calendar=calendar)
    _parked(conn, session)
    old = _token(conn)
    _confirm(conn, "m1", revision=1)
    if change == "calendar":
        calendar.calendar_id = "another-calendar"
    else:
        monkeypatch.setattr("app.policy.hashing.HASH_VERSION", 2)
    announced: list[tuple[str, int]] = []

    applied = apply_open(
        session, announce=lambda record: announced.append((record.message_id, record.generation))
    )

    assert applied == [("m1", "no_effect")]
    assert session.resumes == 0
    assert announced == [("m1", 2)]
    assert _proposal(conn) == ("pending", 1, None)
    assert _outcomes(conn) == [("no_effect", "the proposal changed", True)]
    assert _action(conn) == ("refused", "the proposal changed", False)
    stale = decide(conn, "m1", action="confirm", revision=1, via="web", token=old, dry_run=True)
    assert stale.status == "stale"
    assert _confirm(conn, "m1", revision=1).status == "queued"  # the new card's


@pytest.mark.integration
def test_a_confirm_recorded_before_m17_returns_to_the_owner(conn: psycopg.Connection) -> None:
    """It has no approval: it is shown again ("approve again"), never run."""
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    conn.execute("DELETE FROM outbound_actions WHERE message_id = 'm1'")  # as M16 left it

    assert apply_open(session) == [("m1", "no_effect")]

    assert session.resumes == 0
    assert _outcomes(conn) == [("no_effect", "approve again", True)]
    assert _proposal(conn) == ("pending", 1, None)
    generation = conn.execute("SELECT generation FROM proposals WHERE message_id = 'm1'").fetchone()
    assert generation == (2,)


@pytest.mark.integration
def test_a_confirm_recorded_before_m17_under_the_other_mode_is_expired(
    conn: psycopg.Connection,
) -> None:
    """Returned to the owner it would be shown as if it could run: it is
    expired instead (D2)."""
    calendar = FakeCalendar()
    session = _counting(conn, calendar=calendar)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    conn.execute("DELETE FROM outbound_actions WHERE message_id = 'm1'")  # as M16 left it
    calendar.dry_run = False

    assert apply_open(session) == [("m1", "no_effect")]

    assert _outcomes(conn)[0] == ("no_effect", "made under another mode", True)
    assert conn.execute(
        "SELECT action, reason FROM decisions WHERE message_id = 'm1' AND outcome IS NULL"
    ).fetchone() == ("sweep", "made under another mode")


@pytest.mark.integration
def test_a_failed_settle_refuses_an_approval_left_with_it(conn: psycopg.Connection) -> None:
    """No action outlives its decision, whatever settles it (D2)."""
    session, _ = _world(conn)
    _parked(conn, session)
    result = _confirm(conn, "m1", revision=1)
    assert result.decision_id is not None

    with conn.transaction():
        settle_failed(conn, result.decision_id, "m1", reason="late", action_reason="exhausted")

    assert _action(conn) == ("refused", audit.REASONS["exhausted"], False)


# --- guests outside the thread (M17, 17.8) -------------------------------------------


@pytest.mark.integration
def test_a_guest_no_longer_in_the_thread_sends_the_proposal_back_marked(
    conn: psycopg.Connection,
) -> None:
    """Read again before the Confirm is applied (D4): the proposal comes back
    with the guest marked, and nothing runs."""
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    cast(FakeGmail, session.deps.gmail).thread = {"messages": []}
    announced: list[list[str]] = []

    applied = apply_open(
        session, announce=lambda record: announced.append(record.payload["outside_guests"])
    )

    assert applied == [("m1", "no_effect")]
    assert session.resumes == 0
    assert announced == [["sara@example.com"]]
    assert _outcomes(conn) == [("no_effect", "guests outside the thread", True)]
    assert _action(conn) == ("refused", "guests outside the thread", False)


@pytest.mark.integration
def test_gmail_being_down_holds_a_confirm_without_spending_attempts(
    conn: psycopg.Connection,
) -> None:
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    cast(FakeGmail, session.deps.gmail).down = True

    assert apply_open(session) == [("m1", "held")]

    attempts, wait, leased = cast(tuple[int, float, bool], _open(conn))
    assert (attempts, leased) == (0, False) and 590 < wait <= 600
    assert session.resumes == 0


@pytest.mark.integration
def test_gmail_down_for_an_hour_sends_the_proposal_back_with_every_guest_outside(
    conn: psycopg.Connection,
) -> None:
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    conn.execute("UPDATE decisions SET decided_at = now() - interval '2 hours'")
    cast(FakeGmail, session.deps.gmail).down = True

    assert apply_open(session) == [("m1", "no_effect")]

    payload = conn.execute("SELECT payload FROM proposals WHERE message_id = 'm1'").fetchone()
    assert payload is not None and payload[0]["outside_guests"] == ["sara@example.com"]


@pytest.mark.integration
def test_gmail_down_for_an_hour_still_lets_an_allowed_guest_through(
    conn: psycopg.Connection,
) -> None:
    """A contact the owner allowed may always be invited (D4): no read of
    Gmail is needed for them, at the check or at execution."""
    from app.policy import contacts

    conn.execute("DELETE FROM confirmed_contacts")
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    contacts.allow(conn, "sara@example.com", via="web", key=KEY)
    conn.execute("UPDATE decisions SET decided_at = now() - interval '2 hours'")
    cast(FakeGmail, session.deps.gmail).down = True

    assert apply_open(session) == [("m1", "skipped")]


@pytest.mark.integration
def test_gmail_down_at_execution_holds_without_spending_attempts(
    conn: psycopg.Connection,
) -> None:
    """The check before the Confirm read the thread; the registry's own read,
    a moment later, fails. Nothing ran, and it waits like the first check."""
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    gmail = cast(FakeGmail, session.deps.gmail)
    gmail.down_after = gmail.reads + 1  # the worker's read succeeds; the registry's fails

    assert apply_open(session) == [("m1", "held")]
    attempts, wait, leased = cast(tuple[int, float, bool], _open(conn))
    assert (attempts, leased) == (0, False) and 590 < wait <= 600
    assert _action(conn)[0] == "approved"

    gmail.down_after = None
    _make_due(conn)
    assert apply_open(session) == [("m1", "skipped")]
    assert (session.resumes, session.redrives) == (1, 1)


# --- the spending cap (M17, 17.11) ----------------------------------------------------


@dataclass
class CapGate:
    room: bool = True
    message_room: bool = True

    def allows_new_work(self) -> bool:
        return self.room

    def allows_message(self, message_id: str) -> bool:
        return self.message_room


@pytest.mark.integration
def test_at_the_cap_an_edit_waits_and_costs_nothing(conn: psycopg.Connection) -> None:
    """An edit re-extracts, which calls a model (D5)."""
    session = _counting(conn)
    _parked(conn, session)
    decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")
    session.gate = cast(Any, CapGate(room=False))

    assert apply_open(session) == [("m1", "waiting")]

    assert session.resumes == 0
    attempts, wait, leased = cast(tuple[int, float, bool], _open(conn))
    assert (attempts, leased) == (0, False)
    assert abs(wait - worker.HELD_RETRY.total_seconds()) < 1  # pushed back, not left due


@pytest.mark.integration
def test_at_the_cap_a_cancel_still_applies(conn: psycopg.Connection) -> None:
    """A Cancel, like a Confirm, calls no model."""
    session = _counting(conn)
    _parked(conn, session)
    decide(conn, "m1", action="cancel", revision=1, via="web")
    session.gate = cast(Any, CapGate(room=False))

    assert apply_open(session) == [("m1", "rejected")]


@pytest.mark.integration
def test_at_the_cap_a_confirm_still_applies(conn: psycopg.Connection) -> None:
    session = _counting(conn)
    _parked(conn, session)
    _confirm(conn, "m1", revision=1)
    session.gate = cast(Any, CapGate(room=False))

    assert apply_open(session) == [("m1", "skipped")]  # a dry run, applied


@pytest.mark.integration
def test_an_edit_on_a_message_over_its_ceiling_goes_back_to_the_owner(
    conn: psycopg.Connection,
) -> None:
    """Checked before the resume: the proposal is still parked, so the owner
    can still Confirm or Cancel it, neither of which calls a model."""
    session = _counting(conn)
    _parked(conn, session)
    queued = decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")
    session.gate = cast(Any, CapGate(message_room=False))

    assert apply_open(session) == [("m1", "no_effect")]

    assert session.resumes == 0
    assert _outcomes(conn) == [("no_effect", "too costly to read", True)]
    assert _proposal(conn) == ("pending", 1, None)
    audited = conn.execute(
        "SELECT count(*) FROM audit_log WHERE kind = 'message_too_costly' AND decision_id = %s",
        (queued.decision_id,),
    ).fetchone()
    assert audited == (1,)


@dataclass
class RefusingPipeline(FakePipeline):
    refusal: Exception | None = None

    def extract(self, email: EmailMessage, **kwargs: Any) -> ExtractionResult:
        self.calls += 1
        if self.calls > 1 and self.refusal is not None:
            raise self.refusal
        return _meeting()


@pytest.mark.integration
def test_a_refusal_mid_edit_waits_without_spending_attempts(conn: psycopg.Connection) -> None:
    from app.policy.budget import BudgetExhaustedError

    pipeline = RefusingPipeline(refusal=BudgetExhaustedError("cap"))
    session = _counting(conn, pipeline=pipeline)
    _parked(conn, session)
    decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")

    assert apply_open(session) == [("m1", "waiting")]

    attempts, wait, leased = cast(tuple[int, float, bool], _open(conn))
    assert (attempts, leased) == (0, False)
    assert abs(wait - worker.HELD_RETRY.total_seconds()) < 1


@pytest.mark.integration
def test_a_message_that_reaches_its_ceiling_mid_edit_is_skipped(conn: psycopg.Connection) -> None:
    """Past its interrupt, nothing can finish it: SKIPPED, as poll records a
    message it could not afford, and audited with the settle."""
    from app.policy.budget import MessageTooCostlyError

    pipeline = RefusingPipeline(refusal=MessageTooCostlyError("ceiling"))
    session = _counting(conn, pipeline=pipeline)
    _parked(conn, session)
    decide(conn, "m1", action="edit", revision=1, correction="make it 5pm", via="web")

    assert apply_open(session) == [("m1", "skipped")]

    assert _outcomes(conn) == [("skipped", "too costly to read", True)]
    assert _ledger(conn) is MessageStatus.SKIPPED


@pytest.mark.integration
def test_a_write_known_to_exist_whose_settle_fails_is_left_open_as_an_error(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing it would be untrue, and so would saying it could not be
    confirmed: no `write_unconfirmed`, and the stuck clock reports it."""
    session = _cut_off_and_exhausted(conn, FakeCalendar(dry_run=False, fail_before=1))
    conn.execute("UPDATE outbound_actions SET status = 'done'")

    def unreadable(*args: Any, **kwargs: Any) -> Step:
        raise RuntimeError("the checkpoint cannot be read")

    monkeypatch.setattr(worker, "step_for", unreadable)

    assert apply_open(session) == [("m1", "error")]
    assert _open(conn) is not None
    unconfirmed = conn.execute(
        "SELECT count(*) FROM audit_log WHERE kind = 'write_unconfirmed' AND message_id = 'm1'"
    ).fetchone()
    assert unconfirmed == (0,)


# --- withdraw (M17, 17.12) ------------------------------------------------------------


def _withdrawals(conn: psycopg.Connection, decision_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT kind FROM audit_log
         WHERE decision_id = %s AND kind IN ('decision_withdrawn', 'withdraw_declined')
         ORDER BY id
        """,
        (decision_id,),
    ).fetchall()
    return [row[0] for row in rows]


@pytest.mark.integration
def test_a_withdraw_request_is_recorded_only_on_an_open_decision(
    conn: psycopg.Connection,
) -> None:
    session, _ = _world(conn)
    _parked(conn, session)
    decision = _confirm(conn, "m1", revision=1)

    assert request_withdraw(conn, decision.decision_id) == "requested"
    assert request_withdraw(conn, decision.decision_id) == "requested"  # asking twice
    assert request_withdraw(conn, 2**62) == "not_found"

    conn.execute("UPDATE decisions SET withdraw_requested_at = NULL")
    apply_open(session)
    assert request_withdraw(conn, decision.decision_id) == "settled"


@pytest.mark.integration
def test_an_operators_sweep_cannot_be_withdrawn(conn: psycopg.Connection) -> None:
    """A sweep is never the owner's tap."""
    session, _ = _world(conn)
    _parked(conn, session)
    sweep = decide(conn, "m1", action="sweep", revision=1, via="sweep")
    assert sweep.decision_id is not None

    assert request_withdraw(conn, sweep.decision_id) == "not_found"
    row = conn.execute("SELECT withdraw_requested_at FROM decisions").fetchone()
    assert row == (None,)


@pytest.mark.integration
def test_a_withdrawn_confirm_books_nothing_and_its_old_card_is_stale(
    conn: psycopg.Connection,
) -> None:
    """Seen before anything is applied (D6). The proposal comes back at the
    next generation, so the old card's Confirm dies on it."""
    calendar = FakeCalendar(dry_run=False)
    session = _counting(conn, calendar=calendar)
    _parked(conn, session)
    old = conn.execute("SELECT args_hash, dry_run, generation FROM proposals").fetchone()
    assert old is not None
    decision = _confirm(conn, "m1", revision=1)
    request_withdraw(conn, decision.decision_id)

    assert apply_open(session) == [("m1", "withdrawn")]

    assert (session.resumes, calendar.calls) == (0, 0)
    assert _outcomes(conn) == [("no_effect", audit.REASONS["withdrawn"], True)]
    assert _proposal(conn) == ("pending", 1, None)
    assert _action(conn)[:2] == ("refused", audit.REASONS["withdrawn"])
    assert _withdrawals(conn, decision.decision_id) == ["decision_withdrawn"]
    replay = decide(
        conn,
        "m1",
        action="confirm",
        revision=1,
        via="web",
        token=card_token(old[0], old[1], old[2]),
        dry_run=old[1],
    )
    assert replay.status == "stale"


@pytest.mark.integration
def test_a_withdraw_is_carried_out_while_paused_and_nothing_else_is(
    conn: psycopg.Connection,
) -> None:
    from app.jobs.scheduler import decisions_status

    session = _counting(conn)
    _parked(conn, session)
    decision = _confirm(conn, "m1", revision=1)
    control.switch(conn, paused=True, via="web")

    assert apply_open(session) == []  # paused: the Confirm waits
    assert decisions_status(conn).due is False

    request_withdraw(conn, decision.decision_id)
    assert decisions_status(conn).due is True  # worth a session, even paused

    assert apply_open(session) == [("m1", "withdrawn")]
    assert session.resumes == 0


@pytest.mark.integration
def test_a_withdraw_is_declined_once_the_write_has_begun(conn: psycopg.Connection) -> None:
    """Even with the lease cleared by a failed attempt: the action is under
    way, so the decision goes on, and the owner is told it could not be
    withdrawn."""
    calendar = FakeCalendar(dry_run=False, fail_after=1)
    session = _counting(conn, calendar=calendar)
    _parked(conn, session)
    decision = _confirm(conn, "m1", revision=1)
    assert apply_open(session) == [("m1", "retrying")]
    assert _action(conn)[0] == "executing"
    request_withdraw(conn, decision.decision_id)

    assert apply_open(session) == [("m1", "declined")]

    row = conn.execute(
        "SELECT outcome, withdraw_requested_at, lease_until, attempts FROM decisions"
    ).fetchone()
    assert row == (None, None, None, 1)  # still open; the request cleared; no attempt spent
    assert _withdrawals(conn, decision.decision_id) == ["withdraw_declined"]

    _make_due(conn)
    assert apply_open(session) == [("m1", "created")]  # and it goes on as it was
    assert calendar.inserts == 1


@pytest.mark.integration
def test_a_withdrawn_cancel_returns_the_proposal(conn: psycopg.Connection) -> None:
    session = _counting(conn)
    _parked(conn, session)
    queued = decide(conn, "m1", action="cancel", revision=1, via="web")
    assert queued.decision_id is not None
    request_withdraw(conn, queued.decision_id)

    assert apply_open(session) == [("m1", "withdrawn")]

    assert _proposal(conn) == ("pending", 1, None)
    assert session.resumes == 0
