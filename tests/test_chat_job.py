"""The chat job (M19, D1): a turn stored, run once, answered and traced."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from app.eval.planner import CaseCalendar, CaseMail, load_world
from app.extraction.llm import LlmError
from app.planner import chat, turns
from app.planner.chat import (
    OUT_OF_TIME,
    STRANDED,
    TURN_DEADLINE,
    WENT_WRONG,
    Cards,
    ChatSession,
)
from app.planner.execute import TurnStoppedError
from app.planner.origins import FromPaste
from app.planner.plan import PlanPayload
from app.planner.reader import Reading
from app.planner.render import COULD_NOT_PLAN
from app.policy.budget import (
    CHAT_STOPS,
    TURN_CEILING_STOP,
    BudgetExhaustedError,
    ChatCheck,
    Gate,
    MessageTooCostlyError,
)
from app.policy.hashing import Binding, args_key

WORLD = load_world()

FREE_AT_3_STEPS = [
    {"op": "calendar.freebusy", "start": "2026-10-08T15:00:00", "end": "2026-10-08T16:00:00"}
]
FREE_AT_3 = {
    "kind": "answer",
    "timezone": "Asia/Karachi",
    "steps": FREE_AT_3_STEPS,
    "reply": {"template": "free_busy"},
}
READ_PASTE = {
    "kind": "answer",
    "timezone": "Asia/Karachi",
    "steps": [{"op": "mail.read", "message_ref": "$paste", "ask": "summary"}],
    "reply": {"template": "quote"},
}
HOLD = {
    "kind": "answer",
    "timezone": "Asia/Karachi",
    "steps": [
        {
            "op": "calendar.propose_hold",
            "title": "Focus",
            "start": "2026-10-13T14:00:00",
            "end": "2026-10-13T16:00:00",
        }
    ],
    "reply": {"template": "proposal"},
}


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Planner:
    plan: dict[str, Any] | BaseException = field(default_factory=lambda: dict(FREE_AT_3))
    asked: list[str] = field(default_factory=list)
    then: Callable[[], None] = lambda: None

    def __call__(self, typed: str, pasted: bool, asked_at: datetime) -> PlanPayload:
        self.asked.append(typed)
        self.then()
        if isinstance(self.plan, BaseException):
            raise self.plan
        return PlanPayload.model_validate(self.plan)


class SlowCalendar(CaseCalendar):
    """A free/busy read that takes the turn past its deadline."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(WORLD.events)
        self.clock = clock

    def freebusy(self, start_utc: datetime, end_utc: datetime) -> Any:
        self.clock.now += TURN_DEADLINE
        return super().freebusy(start_utc, end_utc)


def _session(
    conn: psycopg.Connection,
    planner: Planner | None = None,
    *,
    clock: Clock | None = None,
    reader: Callable[..., object] | None = None,
    calendar: Any = None,
    announce: Any = None,
) -> ChatSession:
    clock = clock or Clock(datetime.now(UTC))
    gate = Gate(conn, Decimal(40), Decimal("0.10"), clock=clock)
    return ChatSession(
        conn=conn,
        plan=planner or Planner(),
        reader=lambda: reader or (lambda source, ask: Reading(FromPaste(), (), ())),
        gmail=CaseMail(WORLD.messages),
        calendar=calendar or CaseCalendar(WORLD.events),
        contacts=lambda: list(WORLD.contacts),
        check=ChatCheck(gate, Decimal("1.00"), Decimal("15"), WORLD.zone),
        hours=WORLD.hours,
        zone=WORLD.zone,
        owner=frozenset({WORLD.owner}),
        cards=Cards(
            binding=Binding(calendar_id="test-calendar", key=args_key("test-key")),
            pipeline_version="chat-test",
            dry_run=True,
            announce=announce,
        ),
        clock=clock,
    )


@pytest.fixture
def db(conn: psycopg.Connection) -> psycopg.Connection:
    conn.execute("DELETE FROM chat_turns")  # rolled back with the test
    conn.execute("DELETE FROM model_spend")
    conn.execute("UPDATE control SET paused = false WHERE id = 1")
    return conn


def _ask(conn: psycopg.Connection, typed: str = "Am I free tomorrow at 3pm?", **kwargs: Any) -> int:
    key = kwargs.pop("key", f"key-{typed[:8]}-{len(typed):04d}".replace(" ", "-"))
    return turns.store(
        conn, key=key.ljust(16, "x"), typed=typed, pasted=kwargs.get("pasted")
    ).turn_id


def _turn(conn: psycopg.Connection, turn_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT status, plan, answer, reason, started_at IS NOT NULL, finished_at IS NOT NULL"
        " FROM chat_turns WHERE id = %s",
        (turn_id,),
    ).fetchone()
    assert row is not None
    keys = ("status", "plan", "answer", "reason", "started", "finished")
    return dict(zip(keys, row, strict=True))


# --- a turn, end to end ------------------------------------------------------------------


@pytest.mark.integration
def test_a_turn_is_stored_run_answered_and_traced(db: psycopg.Connection) -> None:
    turn_id = _ask(db)

    assert chat.answer_next(_session(db)) == turn_id

    turn = _turn(db, turn_id)
    assert (turn["status"], turn["reason"], turn["started"], turn["finished"]) == (
        "answered",
        None,
        True,
        True,
    )
    assert turn["plan"]["steps"][0]["op"] == "calendar.freebusy"
    assert turn["answer"]["text"] == (
        "On your primary calendar, Thu 8 Oct 2026 15:00-16:00: you're busy 15:00-16:00."
    )
    run = db.execute(
        "SELECT trace_id, status FROM runs WHERE gmail_message_id = %s", (f"chat-{turn_id}",)
    ).fetchone()
    assert run is not None and run[1] == "success"
    nodes = [row[0] for row in db.execute("SELECT node FROM spans WHERE trace_id = %s", (run[0],))]
    assert nodes == ["plan"]


@pytest.mark.integration
def test_turns_are_answered_one_at_a_time_oldest_first(db: psycopg.Connection) -> None:
    first, second = _ask(db, "First question"), _ask(db, "Second question")
    planner = Planner()
    session = _session(db, planner)

    assert [chat.answer_next(session), chat.answer_next(session)] == [first, second]
    assert chat.answer_next(session) is None
    assert planner.asked == ["First question", "Second question"]


@pytest.mark.integration
def test_a_read_is_traced(db: psycopg.Connection) -> None:
    turn_id = _ask(db, "Summarize this", pasted="Please review the contract.")
    chat.answer_next(_session(db, Planner(dict(READ_PASTE))))

    nodes = db.execute(
        "SELECT node FROM spans JOIN runs USING (trace_id) WHERE gmail_message_id = %s"
        " ORDER BY spans.started_at",
        (f"chat-{turn_id}",),
    ).fetchall()
    assert [node for (node,) in nodes] == ["plan", "read"]


# --- how a turn ends ---------------------------------------------------------------------


@pytest.mark.integration
def test_a_pause_stops_a_turn_before_it_is_planned(db: psycopg.Connection) -> None:
    turn_id = _ask(db)
    db.execute("UPDATE control SET paused = true WHERE id = 1")
    planner = Planner()

    chat.answer_next(_session(db, planner))

    assert planner.asked == []
    assert (_turn(db, turn_id)["status"], _turn(db, turn_id)["reason"]) == (
        "stopped",
        CHAT_STOPS["paused"],
    )


@pytest.mark.integration
def test_the_deadline_stops_a_slow_turn(db: psycopg.Connection) -> None:
    turn_id = _ask(db)
    clock = Clock(datetime.now(UTC))
    two_reads = dict(FREE_AT_3, steps=[*FREE_AT_3_STEPS, *FREE_AT_3_STEPS])

    chat.answer_next(_session(db, Planner(two_reads), clock=clock, calendar=SlowCalendar(clock)))

    turn = _turn(db, turn_id)
    assert (turn["status"], turn["reason"]) == ("stopped", OUT_OF_TIME)
    assert turn["plan"] is not None and turn["answer"] is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "failure",
    [
        {"kind": "answer", "timezone": "Asia/Karachi", "steps": [], "reply": {}},
        LlmError("Response did not match PlanPayload"),
    ],
)
def test_a_plan_that_cannot_run_fails_the_turn(
    db: psycopg.Connection, failure: dict[str, Any] | Exception
) -> None:
    turn_id = _ask(db)
    chat.answer_next(_session(db, Planner(failure)))
    assert (_turn(db, turn_id)["status"], _turn(db, turn_id)["reason"]) == (
        "failed",
        COULD_NOT_PLAN,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("refusal", "reason"),
    [
        (MessageTooCostlyError("ceiling"), TURN_CEILING_STOP),
        (BudgetExhaustedError("cap"), CHAT_STOPS["spending"]),
    ],
)
def test_the_gate_refusing_the_planner_stops_the_turn(
    db: psycopg.Connection, refusal: Exception, reason: str
) -> None:
    turn_id = _ask(db)
    chat.answer_next(_session(db, Planner(refusal)))
    assert (_turn(db, turn_id)["status"], _turn(db, turn_id)["reason"]) == ("stopped", reason)


@pytest.mark.integration
def test_the_gate_refusing_a_read_stops_the_turn_not_the_step(db: psycopg.Connection) -> None:
    turn_id = _ask(db, "Summarize this", pasted="Please review.")

    def refused(source: Any, ask: Any) -> object:
        raise MessageTooCostlyError("ceiling")

    chat.answer_next(_session(db, Planner(dict(READ_PASTE)), reader=refused))

    turn = _turn(db, turn_id)
    assert (turn["status"], turn["reason"]) == ("stopped", TURN_CEILING_STOP)


@pytest.mark.integration
def test_something_unforeseen_fails_the_turn_and_its_trace(db: psycopg.Connection) -> None:
    turn_id = _ask(db)
    chat.answer_next(_session(db, Planner(RuntimeError("unforeseen"))))

    assert (_turn(db, turn_id)["status"], _turn(db, turn_id)["reason"]) == ("failed", WENT_WRONG)
    run = db.execute(
        "SELECT status, error FROM runs WHERE gmail_message_id = %s", (f"chat-{turn_id}",)
    ).fetchone()
    assert run == ("failed", "RuntimeError: unforeseen")


# --- stranded turns ------------------------------------------------------------------------


@pytest.mark.integration
def test_a_turn_running_past_its_deadline_is_failed_as_stranded(db: psycopg.Connection) -> None:
    turn_id = _ask(db)
    db.execute(
        "UPDATE chat_turns SET status = 'running', started_at = now() - interval '2 minutes'"
        " WHERE id = %s",
        (turn_id,),
    )
    young = _ask(db, "A turn just started")
    db.execute(
        "UPDATE chat_turns SET status = 'running', started_at = now() WHERE id = %s", (young,)
    )

    assert chat.strand(db, now=datetime.now(UTC)) == [turn_id]
    assert (_turn(db, turn_id)["status"], _turn(db, turn_id)["reason"]) == ("failed", STRANDED)
    assert _turn(db, young)["status"] == "running"


@pytest.mark.integration
def test_a_stranded_turn_parks_nothing_and_its_end_is_not_written(db: psycopg.Connection) -> None:
    """The sweep fails a turn while its job still runs it: what the job does
    after that is never written (D1)."""
    turn_id = _ask(db, "Block Tuesday from 2 to 4pm")
    announced: list[Any] = []

    def sweep_now() -> None:
        chat.strand(db, now=datetime.now(UTC) + TURN_DEADLINE + timedelta(seconds=1))

    session = _session(db, Planner(dict(HOLD), then=sweep_now), announce=announced.append)
    chat.answer_next(session)

    assert announced == []
    parked = db.execute(
        "SELECT count(*) FROM proposals WHERE chat_turn_id = %s", (turn_id,)
    ).fetchone()
    assert parked == (0,)
    assert (_turn(db, turn_id)["status"], _turn(db, turn_id)["reason"]) == ("failed", STRANDED)


@pytest.mark.integration
def test_a_park_is_held_to_the_turn_still_running(db: psycopg.Connection) -> None:
    turn_id = _ask(db)
    with pytest.raises(TurnStoppedError), chat.while_running(db, turn_id):
        pass  # still queued, never claimed
    assert chat.finish(db, turn_id, "answered") is False
    assert _turn(db, turn_id)["status"] == "queued"


@pytest.mark.integration
def test_a_turns_model_calls_are_metered_under_its_own_id(db: psycopg.Connection) -> None:
    """So the turn's gate holds it to its ceiling, and its day is counted (D9)."""
    from app.policy.models import CURRENT_MESSAGE

    turn_id = _ask(db)
    serving: list[str | None] = []
    planner = Planner(then=lambda: serving.append(CURRENT_MESSAGE.get()))

    chat.answer_next(_session(db, planner))

    assert serving == [f"chat-{turn_id}"]
    assert CURRENT_MESSAGE.get() is None


@pytest.mark.integration
def test_a_question_goes_from_the_api_to_the_job_to_an_answer(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phase's checkpoint: on Postgres, with fakes for Gmail, the calendar
    and the model."""
    from contextlib import contextmanager

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from app.config import Settings
    from app.web_api import router

    @contextmanager
    def the_test_connection(url: str) -> Any:
        yield db

    settings = Settings(
        _env_file=None,
        database_url="postgresql://localhost/test",
        gemini_api_key="test-key",
        web_api_secret=SecretStr("secret-for-tests"),
        user_timezone="Asia/Karachi",
    )
    monkeypatch.setattr("app.web_api.get_settings", lambda: settings)
    monkeypatch.setattr("app.web_api.connect_autocommit", the_test_connection)
    monkeypatch.setattr("app.web_api.chat_requested", lambda: True)
    app = FastAPI()
    app.include_router(router)

    posted = TestClient(app).post(
        "/api/chat",
        json={
            "key": "a-key-of-sixteen-or-more",
            "zone": "Asia/Karachi",
            "parts": [{"text": "Am I free tomorrow at 3pm?", "pasted": False}],
        },
        headers={"Authorization": "Bearer secret-for-tests"},
    )
    assert posted.status_code == 202
    turn_id = posted.json()["turn_id"]

    planner = Planner()
    assert chat.answer_next(_session(db, planner)) == turn_id

    assert planner.asked == ["Am I free tomorrow at 3pm?"]
    turn = _turn(db, turn_id)
    assert turn["status"] == "answered"
    assert turn["answer"]["text"].startswith("On your primary calendar, Thu 8 Oct 2026 15:00")
