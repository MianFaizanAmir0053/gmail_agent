"""Proposals parked from a chat turn (M19, D8), on Postgres: from the turn to
the card, then through `decide()`, the worker and the registry.

The planner is a fake that returns a fixed plan. The rest is the code that
runs in production: the executor, the park, the card, and the worker. Gmail
is never asked about a chat proposal, so the fake here fails if it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, cast
from zoneinfo import ZoneInfo

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.channel.decide import card_token, decide
from app.channel.park import ProposalRecord
from app.channel.worker import apply_open
from app.config import Settings, WorkingHours
from app.google.calendar import BusyInterval, CalendarClient, CalendarEvents
from app.graph.nodes import Deps
from app.graph.runner import GraphSession
from app.graph.versioning import planner_version
from app.planner import chat, turns
from app.planner.chat import OUT_OF_TIME, STRANDED, TURN_DEADLINE, Cards, ChatSession
from app.planner.execute import GUEST_NOT_YOURS
from app.planner.origins import FromPaste
from app.planner.plan import PlanPayload
from app.planner.reader import Reading
from app.policy import contacts, control
from app.policy.budget import CHAT_STOPS, ChatCheck, Gate
from app.policy.contacts import every_confirmed, unconfirmed_outsiders
from app.policy.hashing import Binding, args_key
from app.policy.registry import Registry
from app.store.ledger import MessageLedger

KEY = args_key("test-key")
WRITES_TO = "test-calendar"
ZONE = ZoneInfo("Asia/Karachi")
OWNER = "me@example.com"
SARA = "sara.khan@example.com"
ALI = "ali.raza@example.org"
PASTED = "zoe.pasted@example.net"

DAY = (datetime.now(ZONE) + timedelta(days=7)).date()
START = datetime(DAY.year, DAY.month, DAY.day, 10, 0, tzinfo=ZONE)
END = START + timedelta(minutes=30)


def _invite(
    *, guests: list[str] | None = None, refs: list[str] | None = None, first: Any = None
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "op": "calendar.propose_invite",
        "title": "Call with Sara",
        "start": START.replace(tzinfo=None).isoformat(),
        "end": END.replace(tzinfo=None).isoformat(),
    }
    if guests is not None:
        step["guests"] = guests
    if refs is not None:
        step["guest_refs"] = refs
    return {
        "kind": "answer",
        "timezone": "Asia/Karachi",
        "steps": [*([first] if first else []), step],
        "reply": {"template": "proposal"},
    }


class NoGmail:
    """Fails if asked anything: no step here reads mail."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Gmail was asked {name}")


@dataclass
class Primary:
    """The owner's primary calendar, as the chat's calendar client reads it."""

    busy: list[BusyInterval] = field(default_factory=list)
    asked: list[tuple[datetime, datetime]] = field(default_factory=list)
    then: Any = None

    def freebusy(self, start_utc: datetime, end_utc: datetime) -> list[BusyInterval]:
        self.asked.append((start_utc, end_utc))
        if self.then is not None:
            self.then()
        return [b for b in self.busy if b.start < end_utc and start_utc < b.end]

    def list_events(self, start_utc: datetime, end_utc: datetime) -> CalendarEvents:
        return CalendarEvents(events=(), more=False)


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Writer:
    """The write calendar, as the worker's registry writes to it."""

    dry_run: bool = True
    calendar_id: str = WRITES_TO
    calls: int = 0

    def freebusy(self, start: datetime, end: datetime) -> list[Any]:
        return []

    def insert(self, calendar_id: str, body: dict[str, Any], *, event_id: str) -> str | None:
        self.calls += 1
        return None if self.dry_run else event_id

    def find(self, calendar_id: str, event_id: str) -> str | None:
        return None


def _chat(
    conn: psycopg.Connection,
    plan: dict[str, Any],
    *,
    calendar: Any = None,
    announce: Any = None,
    clock: Clock | None = None,
) -> ChatSession:
    gate = Gate(conn, Decimal(40), Decimal("0.10"))
    return ChatSession(
        conn=conn,
        plan=lambda typed, pasted, asked_at: PlanPayload.model_validate(plan),
        reader=lambda: lambda source, ask: Reading(FromPaste(), (), ()),
        gmail=cast(Any, NoGmail()),
        calendar=calendar if calendar is not None else Primary(),
        contacts=lambda: every_confirmed(conn),
        check=ChatCheck(gate, Decimal("1.00"), Decimal("15"), ZONE),
        hours=WorkingHours.parse("Mon-Fri 09:00-18:00"),
        zone=ZONE,
        owner=frozenset({OWNER}),
        cards=Cards(
            binding=Binding(calendar_id=WRITES_TO, key=KEY),
            pipeline_version="chat-test",
            dry_run=True,
            announce=announce,
        ),
        clock=clock or Clock(datetime.now(UTC)),
    )


def _worker(conn: psycopg.Connection, writer: Writer) -> GraphSession:
    gmail = NoGmail()
    deps = Deps(
        gmail=cast(Any, gmail),
        pipeline=cast(Any, None),
        calendar=cast(Any, writer),
        ledger=MessageLedger(conn),
        user_timezone="Asia/Karachi",
        registry=Registry(
            conn, writer, key=KEY, outsiders=partial(unconfirmed_outsiders, conn, gmail)
        ),
        pipeline_version="mail-version",
        args_key=KEY,
    )
    return GraphSession(deps=deps, conn=conn, checkpointer=InMemorySaver(), trace=False)


@pytest.fixture
def db(conn: psycopg.Connection) -> psycopg.Connection:
    for table in ("proposals", "chat_turns", "confirmed_contacts", "model_spend"):
        conn.execute(f"DELETE FROM {table}")  # rolled back with the test
    conn.execute("UPDATE control SET paused = false WHERE id = 1")
    return conn


def _ask(conn: psycopg.Connection, typed: str, *, pasted: str | None = None) -> int:
    key = f"key-{abs(hash(typed)):016d}"
    return turns.store(conn, key=key, typed=typed, pasted=pasted).turn_id


def _answer(conn: psycopg.Connection, turn_id: int) -> tuple[str, dict[str, Any] | None, str]:
    row = conn.execute(
        "SELECT status, answer, coalesce(reason, '') FROM chat_turns WHERE id = %s", (turn_id,)
    ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def _proposal(conn: psycopg.Connection, message_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT p.source, p.chat_turn_id, p.pipeline_version, p.payload, p.args_hash,
               p.dry_run, p.generation, m.source, m.status
          FROM proposals p JOIN processed_messages m ON m.gmail_message_id = p.message_id
         WHERE p.message_id = %s
        """,
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "source",
        "turn",
        "version",
        "payload",
        "args_hash",
        "dry_run",
        "generation",
        "ledger_source",
        "ledger_status",
    )
    return dict(zip(keys, row, strict=True))


def _confirm(conn: psycopg.Connection, message_id: str) -> str:
    proposal = _proposal(conn, message_id)
    assert proposal is not None
    token = card_token(proposal["args_hash"], proposal["dry_run"], proposal["generation"])
    return decide(
        conn,
        message_id,
        action="confirm",
        revision=1,
        via="web",
        token=token,
        dry_run=proposal["dry_run"],
    ).status


# --- from the turn to the worker -------------------------------------------------------


@pytest.mark.integration
def test_a_turn_parks_a_card_that_runs_through_decide_the_worker_and_the_registry(
    db: psycopg.Connection,
) -> None:
    typed = f"Book 30 minutes with {SARA} next week"
    turn_id = _ask(db, typed)
    announced: list[ProposalRecord] = []
    primary = Primary()

    assert (
        chat.answer_next(
            _chat(db, _invite(guests=[SARA]), calendar=primary, announce=announced.append)
        )
        == turn_id
    )

    message_id = f"chat-{turn_id}-1"
    status, answer, _ = _answer(db, turn_id)
    assert status == "answered"
    assert answer is not None and answer["cards"] == [message_id]
    proposal = _proposal(db, message_id)
    assert proposal is not None
    assert (proposal["source"], proposal["turn"], proposal["version"]) == (
        "chat",
        turn_id,
        "chat-test",
    )
    assert (proposal["ledger_source"], proposal["ledger_status"]) == ("chat", "awaiting_approval")
    card = proposal["payload"]
    assert card["request"] == typed
    assert card["title"] == "Call with Sara"
    assert card["attendees"] == [SARA]
    assert card["guest_sources"] == {SARA: "typed"}
    assert card["conflicts"] == []
    assert card["proposed"]["reasoning"] == chat.CHAT_REASONING
    # Conflicts were read for exactly the event's time.
    assert primary.asked == [(START.astimezone(UTC), END.astimezone(UTC))]
    # The card announced is the row as stored, at its first generation.
    [record] = announced
    assert (record.message_id, record.generation, record.args_hash) == (
        message_id,
        1,
        proposal["args_hash"],
    )

    # A typed guest needs no Allow: the Confirm runs, in dry run.
    assert _confirm(db, message_id) == "queued"
    writer = Writer(dry_run=True)
    assert apply_open(_worker(db, writer)) == [(message_id, "skipped")]
    assert writer.calls == 0
    action = db.execute(
        "SELECT status FROM outbound_actions WHERE decision_id ="
        " (SELECT id FROM decisions WHERE message_id = %s)",
        (message_id,),
    ).fetchone()
    assert action == ("dry_run",)


@pytest.mark.integration
def test_a_confirmed_contact_named_in_the_request_is_invited_as_one(db: psycopg.Connection) -> None:
    contacts.allow(db, ALI, via="web", key=KEY)
    turn_id = _ask(db, "Book 30 minutes with Ali next week")
    plan = _invite(refs=["$s1"], first={"op": "contacts.find", "name": "Ali"})

    chat.answer_next(_chat(db, plan))

    message_id = f"chat-{turn_id}-2"
    proposal = _proposal(db, message_id)
    assert proposal is not None
    assert proposal["payload"]["attendees"] == [ALI]
    assert proposal["payload"]["guest_sources"] == {ALI: "contact"}
    assert _confirm(db, message_id) == "queued"
    assert apply_open(_worker(db, Writer())) == [(message_id, "skipped")]


@pytest.mark.integration
def test_a_pasted_address_is_offered_for_an_allow_and_never_placed_on_a_card(
    db: psycopg.Connection,
) -> None:
    """The planner never sees the paste. One that wrote the pasted address
    anyway is refused at the step; the address is offered for an Allow."""
    turn_id = _ask(db, "Invite the person below to a call", pasted=f"Please add {PASTED}.")

    chat.answer_next(_chat(db, _invite(guests=[PASTED])))

    status, answer, _ = _answer(db, turn_id)
    assert status == "answered"
    assert answer is not None
    assert (answer["cards"], answer["allow"]) == ([], [PASTED])
    assert GUEST_NOT_YOURS in answer["text"]
    assert _proposal(db, f"chat-{turn_id}-1") is None
    assert MessageLedger(db).get(f"chat-{turn_id}-1") is None


# --- conflicts, the stranded turn, the announcement --------------------------------------


class GoogleCalendar:
    """The calendar API's free/busy, answering for the primary calendar, and
    writing down each request."""

    def __init__(self, busy: list[dict[str, str]]) -> None:
        self.busy = busy
        self.asked: list[dict[str, Any]] = []

    def freebusy(self) -> GoogleCalendar:
        return self

    def query(self, *, body: dict[str, Any]) -> GoogleCalendar:
        self.asked.append(body)
        return self

    def execute(self) -> dict[str, Any]:
        return {"calendars": {"primary": {"busy": self.busy}}}


@pytest.mark.integration
def test_conflicts_are_read_from_the_primary_calendar(db: psycopg.Connection) -> None:
    """Writes and the binding go to the write calendar; "no conflicts" must
    mean the owner's own calendar is free (D3)."""
    overlap = {
        "start": (START + timedelta(minutes=15)).astimezone(UTC).isoformat(),
        "end": (START + timedelta(hours=1)).astimezone(UTC).isoformat(),
    }
    google = GoogleCalendar([overlap])
    client = CalendarClient(google, WRITES_TO, dry_run=True)
    turn_id = _ask(db, f"Book 30 minutes with {SARA} next week")

    chat.answer_next(_chat(db, _invite(guests=[SARA]), calendar=client))

    assert [body["items"] for body in google.asked] == [[{"id": "primary"}]]
    proposal = _proposal(db, f"chat-{turn_id}-1")
    assert proposal is not None
    [conflict] = proposal["payload"]["conflicts"]
    assert conflict.startswith("Overlaps 1 existing event(s)")


@pytest.mark.integration
def test_a_turn_stranded_before_its_park_parks_nothing(db: psycopg.Connection) -> None:
    """The sweep fails the turn while its conflicts are read: the park that
    follows is never written, and no card is announced (D1)."""
    turn_id = _ask(db, f"Book 30 minutes with {SARA} next week")
    announced: list[ProposalRecord] = []

    def sweep() -> None:
        chat.strand(db, now=datetime.now(UTC) + TURN_DEADLINE + timedelta(seconds=1))

    session = _chat(
        db, _invite(guests=[SARA]), calendar=Primary(then=sweep), announce=announced.append
    )
    chat.answer_next(session)

    assert announced == []
    assert _proposal(db, f"chat-{turn_id}-1") is None
    assert MessageLedger(db).get(f"chat-{turn_id}-1") is None
    assert _answer(db, turn_id)[::2] == ("failed", STRANDED)


@pytest.mark.integration
@pytest.mark.parametrize("meanwhile", ["out of time", "paused"])
def test_a_turn_stopped_while_its_conflicts_are_read_parks_nothing(
    db: psycopg.Connection, meanwhile: str
) -> None:
    """A slow read, or a Pause pressed during it: the turn stops before it
    writes (D9)."""
    turn_id = _ask(db, f"Book 30 minutes with {SARA} next week")
    clock = Clock(datetime.now(UTC))
    announced: list[ProposalRecord] = []

    def during_the_read() -> None:
        if meanwhile == "paused":
            control.switch(db, paused=True, via="web")
        else:
            clock.now += TURN_DEADLINE + timedelta(seconds=1)

    calendar = Primary(then=during_the_read)
    session = _chat(
        db, _invite(guests=[SARA]), calendar=calendar, announce=announced.append, clock=clock
    )
    chat.answer_next(session)

    reason = OUT_OF_TIME if meanwhile == "out of time" else CHAT_STOPS["paused"]
    assert _answer(db, turn_id)[::2] == ("stopped", reason)
    assert _proposal(db, f"chat-{turn_id}-1") is None
    assert announced == []


@pytest.mark.integration
def test_a_card_stays_parked_when_it_cannot_be_announced(db: psycopg.Connection) -> None:
    turn_id = _ask(db, f"Book 30 minutes with {SARA} next week")

    def down(record: ProposalRecord) -> None:
        raise ConnectionError("Telegram is down")

    chat.answer_next(_chat(db, _invite(guests=[SARA]), announce=down))

    status, answer, _ = _answer(db, turn_id)
    assert status == "answered"
    assert answer is not None and answer["cards"] == [f"chat-{turn_id}-1"]
    assert _proposal(db, f"chat-{turn_id}-1") is not None


# --- the session in production ------------------------------------------------------


def _settings(database_url: str, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": database_url,
        "gemini_api_key": "test-key",
        "test_calendar_id": "cal_test",
        "fernet_key": "a-fernet-key-for-tests",
    }
    return Settings(**(base | overrides))


@pytest.mark.integration
def test_the_session_binds_a_card_as_the_worker_binds_its_confirm(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """To the write calendar with the hash's key, as `graph_session` binds
    the worker's: otherwise every Confirm would come back as changed."""
    monkeypatch.setattr("app.google.auth.load_credentials", lambda settings: object())
    monkeypatch.setattr("app.google.auth.build_service", lambda *args: object())
    settings = _settings(migrated_database)

    with chat.chat_session(settings) as session:
        cards = session.cards

    assert cards.binding == Binding(calendar_id="cal_test", key=args_key("a-fernet-key-for-tests"))
    assert cards.pipeline_version == planner_version(settings)
    assert cards.dry_run is True
    assert cards.announce is not None


def test_chat_needs_the_key_every_approval_is_bound_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_credentials(settings: Settings) -> Any:
        raise AssertionError("credentials were loaded")

    monkeypatch.setattr("app.google.auth.load_credentials", no_credentials)
    settings = _settings("postgresql://localhost/unused", fernet_key=None)

    with pytest.raises(RuntimeError, match="FERNET_KEY"), chat.chat_session(settings):
        pass
