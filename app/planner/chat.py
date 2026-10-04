"""The chat job (M19, D1): queued turns answered one at a time.

- **A turn** is claimed from the queue, checked against spending, planned from
  the owner's typed request, run by the executor, rendered by code, and
  answered. The planner and the reader are the only model calls; each is
  metered under `chat-<turn>`, against the turn's own ceiling (D9).
- **Writes are conditional.** Every write after the claim applies only while
  the turn is `running` (`finish`), and a card is parked inside a transaction
  that holds the turn `running` (`while_running`). A turn the sweep has failed
  can never be answered, nor park a card, afterwards.
- **The deadline.** A turn has `TURN_DEADLINE`. Past it, it stops before its
  next step, and says so.
- **Stranded turns.** A turn found `running` past its deadline -- by the job's
  sweep, which also runs at boot -- was left by a restart. It is failed, and
  never retried: the owner asks again.
- **Traced.** Each turn is a run in `runs` under `chat-<turn>`, with a span for
  the plan and for each read, so `/costs` and M15's report include chat.
- **No cards yet.** Until the queue knows a chat proposal from mail's (task
  19.12), a proposal step is refused with a fixed reason.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from app.config import Settings, WorkingHours
from app.extraction.llm import LlmError
from app.obs.trace import Tracer
from app.planner.execute import (
    STOPS_THE_TURN,
    StepRefusedError,
    Tools,
    Turn,
    TurnStoppedError,
    execute,
)
from app.planner.ops import FoundMessage, MailReads
from app.planner.origins import PastedText
from app.planner.plan import Ask, InvalidPlanError, PlanPayload, to_plan
from app.planner.render import COULD_NOT_PLAN, render
from app.policy import models
from app.policy.budget import (
    CHAT_STOPS,
    TURN_CEILING_STOP,
    ChatCheck,
    MessageTooCostlyError,
    chat_message_id,
)

log = logging.getLogger(__name__)

TURN_DEADLINE = timedelta(seconds=90)
"""A turn's time from its claim to its answer, inside the process's kill
timeout (D1)."""

CHAT_EVERY = timedelta(seconds=30)
"""How often the job looks for queued turns. A turn stored in this process
wakes it at once; this bounds the wait otherwise."""

STRANDED = "This was stopped by a restart. Please ask again."
OUT_OF_TIME = "This took too long, so I stopped. Please ask again."
WENT_WRONG = "Something went wrong, so I stopped. Please ask again."
NO_CARDS_YET = "I can't make a card from chat yet."

Status = Literal["answered", "stopped", "failed"]


@dataclass(frozen=True, slots=True)
class Claimed:
    id: int
    typed: str
    pasted: str | None
    asked_at: datetime
    started_at: datetime


# --- the record ------------------------------------------------------------------------


def claim(conn: psycopg.Connection) -> Claimed | None:
    """The oldest queued turn, now `running`; None when the queue is empty."""
    row = conn.execute(
        """
        UPDATE chat_turns SET status = 'running', started_at = now()
         WHERE id = (
               SELECT id FROM chat_turns WHERE status = 'queued'
                ORDER BY asked_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
         )
        RETURNING id, typed, pasted, asked_at, started_at
        """
    ).fetchone()
    if row is None:
        return None
    return Claimed(int(row[0]), row[1] or "", row[2], row[3], row[4])


def finish(
    conn: psycopg.Connection,
    turn_id: int,
    status: Status,
    *,
    plan: dict[str, Any] | None = None,
    answer: dict[str, Any] | None = None,
    reason: str | None = None,
) -> bool:
    """End a running turn. False when it was no longer running: the sweep had
    already failed it, and nothing is written."""
    row = conn.execute(
        """
        UPDATE chat_turns
           SET status = %s, plan = %s, answer = %s, reason = %s, finished_at = now()
         WHERE id = %s AND status = 'running'
        RETURNING id
        """,
        (
            status,
            Jsonb(plan) if plan is not None else None,
            Jsonb(answer) if answer is not None else None,
            reason,
            turn_id,
        ),
    ).fetchone()
    return row is not None


def strand(conn: psycopg.Connection, *, now: datetime) -> list[int]:
    """Fail every turn still `running` past its deadline: a restart left it."""
    rows = conn.execute(
        """
        UPDATE chat_turns SET status = 'failed', reason = %s, finished_at = now()
         WHERE status = 'running' AND started_at < %s
        RETURNING id
        """,
        (STRANDED, now - TURN_DEADLINE),
    ).fetchall()
    return [int(row[0]) for row in rows]


def has_queued(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM chat_turns WHERE status = 'queued')"
    ).fetchone()
    return bool(row and row[0])


@contextmanager
def while_running(conn: psycopg.Connection, turn_id: int) -> Iterator[None]:
    """A transaction that holds the turn `running`, for a park: once the sweep
    has failed a turn, nothing it does is written (D1)."""
    with conn.transaction():
        row = conn.execute(
            "SELECT 1 FROM chat_turns WHERE id = %s AND status = 'running' FOR UPDATE",
            (turn_id,),
        ).fetchone()
        if row is None:
            raise TurnStoppedError(STRANDED)
        yield


# --- one turn --------------------------------------------------------------------------

Planner = Callable[[str, bool, datetime], PlanPayload]
"""The typed request, whether anything was pasted, when it was asked."""

Reading = Callable[[FoundMessage | PastedText, Ask], object]
Parker = Callable[[psycopg.Connection, int, Any], str]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ChatSession:
    """Everything a turn needs, built once per tick."""

    conn: psycopg.Connection
    plan: Planner
    reader: Callable[[], Reading]
    """A fresh reader for each turn, so its limit counts per turn (D6)."""
    gmail: MailReads
    calendar: Any
    contacts: Callable[[], list[str]]
    check: ChatCheck
    hours: WorkingHours
    zone: ZoneInfo
    owner: frozenset[str]
    park: Parker | None = None
    """None until the queue knows chat's proposals (19.12): each is refused."""
    clock: Callable[[], datetime] = _utcnow
    trace: bool = True


def answer_next(session: ChatSession) -> int | None:
    """Claim and answer the oldest queued turn. Its id, or None if none waited."""
    turn = claim(session.conn)
    if turn is None:
        return None
    answer_turn(session, turn)
    return turn.id


def answer_turn(session: ChatSession, turn: Claimed) -> None:
    """Run one claimed turn to its end, whatever happens in it."""
    message_id = chat_message_id(turn.id)
    tracer = Tracer(session.conn) if session.trace else None
    if tracer is not None:
        tracer.start_run(message_id)
    serving = models.CURRENT_MESSAGE.set(message_id)
    failure: BaseException | None = None
    try:
        status, written = _run(session, turn, tracer)
    except Exception as exc:  # the turn ends, whatever went wrong in it
        log.warning("chat turn %d failed: %s", turn.id, type(exc).__name__)
        failure = exc
        status, written = "failed", {"reason": WENT_WRONG}
    finally:
        models.CURRENT_MESSAGE.reset(serving)
    if not finish(session.conn, turn.id, status, **written):
        log.info("chat turn %d was no longer running; its end was not written", turn.id)
    if tracer is not None:
        tracer.finish_run("failed" if status == "failed" else "success", failure)


def _run(
    session: ChatSession, turn: Claimed, tracer: Tracer | None
) -> tuple[Status, dict[str, Any]]:
    deadline = turn.started_at + TURN_DEADLINE

    def go_on() -> None:
        """Before each model call, each park and each step: the deadline and
        the chat check (D9)."""
        if session.clock() >= deadline:
            raise TurnStoppedError(OUT_OF_TIME)
        stop = session.check.stop()
        if stop is not None:
            raise TurnStoppedError(CHAT_STOPS[stop])

    try:
        go_on()
        with _span(tracer, "plan", turn.id):
            payload = session.plan(turn.typed, bool(turn.pasted), turn.asked_at)
        plan = to_plan(payload, pasted=bool(turn.pasted))
    except (InvalidPlanError, LlmError):
        return "failed", {"reason": COULD_NOT_PLAN}
    except STOPS_THE_TURN as stop:
        return "stopped", {"reason": _why(stop)}
    written: dict[str, Any] = {"plan": payload.model_dump(mode="json")}

    reader = session.reader()

    def read(source: FoundMessage | PastedText, ask: Ask) -> object:
        go_on()
        with _span(tracer, "read", turn.id):
            return reader(source, ask)

    def park(proposal: Any) -> str:
        go_on()
        if session.park is None:
            raise StepRefusedError(NO_CARDS_YET)
        with while_running(session.conn, turn.id):
            return session.park(session.conn, turn.id, proposal)

    tools = Tools(
        gmail=session.gmail,
        calendar=session.calendar,
        contacts=session.contacts,
        read=read,
        park=park,
        hours=session.hours,
    )
    owner = Turn(typed=turn.typed, pasted=turn.pasted, owner=session.owner, zone=session.zone)
    try:
        outcome = execute(plan, owner, tools, now=session.clock(), before_step=go_on)
    except STOPS_THE_TURN as stop:
        return "stopped", written | {"reason": _why(stop)}

    answer = render(plan, outcome, zone=session.zone)
    return "answered", written | {
        "answer": {"text": answer.text, "allow": list(answer.allow), "cards": list(answer.cards)}
    }


def _why(stop: BaseException) -> str:
    """The fixed reason a stopped turn gives."""
    if isinstance(stop, TurnStoppedError):
        return stop.reason
    if isinstance(stop, MessageTooCostlyError):
        return TURN_CEILING_STOP
    return CHAT_STOPS["spending"]


@contextmanager
def _span(tracer: Tracer | None, node: str, turn_id: int) -> Iterator[None]:
    if tracer is None:
        yield
        return
    with tracer.span(node, payload={"message_id": chat_message_id(turn_id)}):
        yield


# --- the session in production -------------------------------------------------------


@contextmanager
def chat_session(settings: Settings) -> Iterator[ChatSession]:
    """Open what a tick needs: one connection, the turn's own gate and its
    metered client, Gmail, and the calendar with its read calendar."""
    if settings.test_calendar_id is None:
        raise RuntimeError("TEST_CALENDAR_ID must be set before chat can propose.")

    from app.extraction.llm import GenaiLike
    from app.google.auth import build_service, load_credentials
    from app.google.calendar import CalendarClient
    from app.google.gmail import GmailClient
    from app.planner.plan import make_plan
    from app.planner.reader import Reader
    from app.policy.contacts import every_confirmed

    credentials = load_credentials(settings)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        gate = models.chat_gate(settings, conn)
        client = cast(GenaiLike, models.client(settings, gate))
        gmail = GmailClient(build_service("gmail", "v1", credentials))
        calendar = CalendarClient(
            build_service("calendar", "v3", credentials),
            settings.test_calendar_id,
            dry_run=settings.dry_run,
        )
        zone = ZoneInfo(settings.user_timezone)

        def plan(typed: str, pasted: bool, asked_at: datetime) -> PlanPayload:
            return make_plan(
                client,
                model=settings.planner_model,
                typed=typed,
                pasted=pasted,
                asked_at=asked_at,
                zone=settings.user_timezone,
            ).parsed

        yield ChatSession(
            conn=conn,
            plan=plan,
            reader=lambda: Reader(client=client, model=settings.planner_model, gmail=gmail),
            gmail=gmail,
            calendar=calendar,
            contacts=lambda: every_confirmed(conn),
            check=models.chat_check(settings, gate),
            hours=settings.hours,
            zone=zone,
            owner=frozenset({settings.owner_email, *settings.owner_aliases} - {""}),
        )
