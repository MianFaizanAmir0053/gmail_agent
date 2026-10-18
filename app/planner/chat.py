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
- **Cards.** A proposal step parks a chat proposal (D8). Its conflicts are
  read from the primary calendar; it is claimed and parked while the turn is
  held `running`; then it is announced. The worker takes it from there, as it
  takes mail's.
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

from app.channel.park import Announce, chat_proposal_id, park_chat
from app.config import Settings, WorkingHours
from app.contracts import ExtractionResult
from app.extraction.llm import LlmError, UnavailableError
from app.obs.trace import Tracer
from app.planner.execute import (
    STOPS_THE_TURN,
    Parked,
    Proposal,
    Tools,
    Turn,
    TurnStoppedError,
    execute,
)
from app.planner.ops import FoundMessage, MailReads
from app.planner.origins import PastedText
from app.planner.plan import Ask, InvalidPlanError, PlanPayload, to_plan
from app.planner.render import COULD_NOT_PLAN, cards_made, render
from app.policy import contacts, models
from app.policy.budget import (
    CHAT_STOPS,
    TURN_CEILING_STOP,
    ChatCheck,
    MessageTooCostlyError,
    chat_message_id,
)
from app.policy.hashing import Binding, event_args
from app.tools.calendar_tool import check_conflicts

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
MODEL_UNAVAILABLE = "The model could not be reached just now. Please ask again in a few minutes."
NOT_STARTED = "This could not be started in time. Please ask again."

QUEUE_DEADLINE = timedelta(minutes=10)
"""How long a turn may wait to start. One still queued after this was never
taken up -- the chat job cannot open its session -- and is failed, so the
page unlocks and retention can clear it."""

CARD_LEFT = "A card from this request is waiting for your Confirm."
CARDS_LEFT = "Cards from this request are waiting for your Confirm."
"""The answer of a turn that failed or was stranded after it parked: its cards
wait on the queue all the same, and asking again would make more."""

ASK_AGAIN = " Please ask again."
"""Ends the reasons a turn gives when it stops early, unless it left cards."""

CHAT_REASONING = "Proposed from a chat turn."
"""The stored event's reasoning. No model wrote the event: the executor
built it from the checked plan (D8)."""

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
    """Fail every turn left behind: one still `running` past its deadline,
    which a restart left, and one still `queued` past `QUEUE_DEADLINE`, which
    the job never took up. A stranded turn that had parked a card answers
    with it (`cards_left`)."""
    with conn.transaction():
        rows = conn.execute(
            """
            UPDATE chat_turns
               SET status = 'failed', finished_at = now(),
                   started_at = coalesce(started_at, now()),
                   reason = CASE WHEN status = 'running' THEN %s ELSE %s END
             WHERE (status = 'running' AND started_at < %s)
                OR (status = 'queued' AND asked_at < %s)
            RETURNING id, reason
            """,
            (STRANDED, NOT_STARTED, now - TURN_DEADLINE, now - QUEUE_DEADLINE),
        ).fetchall()
        for turn_id, reason in rows:
            left = cards_left(conn, int(turn_id)) if reason == STRANDED else None
            if left is not None:
                conn.execute(
                    "UPDATE chat_turns SET answer = %s, reason = %s WHERE id = %s",
                    (Jsonb(left), left_cards(reason), turn_id),
                )
    return [int(row[0]) for row in rows]


def left_cards(reason: str) -> str:
    """The reason of a turn that ended after it parked: without "please ask
    again", which would make the cards twice."""
    return reason.removesuffix(ASK_AGAIN)


def cards_left(conn: psycopg.Connection, turn_id: int) -> dict[str, Any] | None:
    """The answer of a turn that ended before it could give its own: the
    cards it parked, which wait on the queue all the same. None if it parked
    none."""
    rows = conn.execute(
        "SELECT message_id FROM proposals WHERE chat_turn_id = %s ORDER BY message_id",
        (turn_id,),
    ).fetchall()
    if not rows:
        return None
    cards = [str(row[0]) for row in rows]
    return {"text": CARD_LEFT if len(cards) == 1 else CARDS_LEFT, "allow": [], "cards": cards}


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


@dataclass(frozen=True)
class Cards:
    """How a turn parks its proposals (D8): what each is bound to, the version
    M24 counts it under, the mode it is made in, and who is told of it."""

    binding: Binding
    pipeline_version: str
    dry_run: bool
    announce: Announce | None = None


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
    cards: Cards
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
        try:
            left = cards_left(session.conn, turn.id)
        except psycopg.Error:  # the failure may be the database's own
            left = None
        if left is not None:
            written = {"reason": left_cards(WENT_WRONG), "answer": left}
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
    except UnavailableError:
        return "failed", {"reason": MODEL_UNAVAILABLE}
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

    parked: list[Parked] = []

    def park(proposal: Proposal) -> str:
        made = _park(session, turn, proposal, go_on=go_on)
        parked.append(Parked(made, proposal))
        return made

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
        stopped = written | {"reason": _why(stop)}
        if parked:
            # A card made before the stop waits on the queue all the same.
            made = cards_made(parked, zone=session.zone)
            stopped["answer"] = {"text": made.text, "allow": [], "cards": list(made.cards)}
            stopped["reason"] = left_cards(_why(stop))
        return "stopped", stopped

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


def _park(
    session: ChatSession, turn: Claimed, proposal: Proposal, *, go_on: Callable[[], None]
) -> str:
    """Park one proposal step as a chat proposal (D8), and return its id.

    The event is held as an `ExtractionResult`, so the hash and the registry
    read it as they read mail's. Its conflicts are read from the primary
    calendar before the turn's row is locked, so no lock waits on Google.
    The deadline and the chat check then run again (`go_on`), so a slow
    read or a Pause pressed meanwhile stops the turn before it writes (D9).
    The claim and the park run while the turn is held `running`, and the
    card is announced once they are committed."""
    cards = session.cards
    proposed = ExtractionResult(
        is_meeting=True,
        title=proposal.title,
        start_utc=proposal.start,
        end_utc=proposal.end,
        timezone=proposal.timezone,
        attendees=[guest.address for guest in proposal.guests],
        confidence=1.0,
        reasoning=CHAT_REASONING,
    )
    message_id = chat_proposal_id(turn.id, proposal.step)
    check = check_conflicts(session.calendar, event_args(proposed, message_id))
    go_on()
    with while_running(session.conn, turn.id):
        # Only confirmed contacts are inside (D8, as amended on 2026-10-18):
        # an address from the request waits for the owner's Allow.
        outsiders = contacts.unconfirmed(session.conn, proposed.attendees)
        record = park_chat(
            session.conn,
            turn_id=turn.id,
            step=proposal.step,
            proposed=proposed,
            request=turn.typed,
            guest_sources={guest.address: guest.source for guest in proposal.guests},
            outside_guests=outsiders,
            conflicts=[check.describe()] if check.has_conflict else [],
            pipeline_version=cards.pipeline_version,
            binding=cards.binding,
            dry_run=cards.dry_run,
        )
    if cards.announce is not None:
        try:
            cards.announce(record)
        except Exception:
            # Parked, and the timeline shows it: a channel being down must
            # not undo that.
            log.exception("could not announce %s", record.message_id)
    return record.message_id


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
    metered client, Gmail, the calendar with its read calendar, and what a
    card is bound to and announced by."""
    if settings.test_calendar_id is None:
        raise RuntimeError("TEST_CALENDAR_ID must be set before chat can propose.")
    if settings.fernet_key is None:
        raise RuntimeError("FERNET_KEY must be set: it keys the hash every approval binds.")

    from app.channel.channels import configured_channels
    from app.extraction.llm import GenaiLike
    from app.google.auth import build_service, load_credentials
    from app.google.calendar import CalendarClient
    from app.google.gmail import GmailClient
    from app.graph.versioning import planner_version
    from app.planner.plan import make_plan
    from app.planner.reader import Reader
    from app.policy.contacts import every_confirmed
    from app.policy.hashing import args_key

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
            cards=Cards(
                # The worker binds a Confirm to the same calendar and key.
                binding=Binding(
                    calendar_id=calendar.calendar_id,
                    key=args_key(settings.fernet_key.get_secret_value()),
                ),
                pipeline_version=planner_version(settings),
                dry_run=settings.dry_run,
                announce=configured_channels(settings).announce,
            ),
        )
