"""The executor: a checked plan, run one step at a time (M19, D4).

Every value carries an origin (`origins.Origin`), and before each step the
executor checks D4's rules against the values the step is handed:
- a read takes only a message a search found, or the owner's paste;
- free time is looked for only in the calendar's own free/busy;
- a slot is one the owner typed, or one the calendar showed free;
- a guest is an address the owner typed (D5), or a confirmed contact.

A search's fields and a proposal's title can only be what the planner wrote,
from the typed request: no reference can fill them, so no step's result can.
The plan's types hold that rule, and a test holds the types.

A step that breaks a rule, or cannot run, is refused with a fixed reason, and
so is every step that needs its result. The other steps still run, and the
turn answers what it could. A step that needs the owner -- a name that matched
no confirmed contact, or several -- ends the turn with its question.

Nothing here writes to a calendar. A proposal is parked, for the owner's
Confirm (D8).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from app.config import WorkingHours
from app.google.calendar import BusyInterval, CalendarEvents
from app.planner.ops import FoundMessage, MailReads, Unclear, find_contact, first_free, search
from app.planner.origins import (
    Origin,
    PastedText,
    computed,
    pasted_addresses,
    typed_addresses,
)
from app.planner.plan import (
    Ask,
    FindContact,
    FirstFree,
    FreeBusy,
    ListEvents,
    Pasted,
    Plan,
    ProposeHold,
    ProposeInvite,
    Read,
    Ref,
    Search,
    Slot,
    Step,
)
from app.policy.participants import guest_key

log = logging.getLogger(__name__)

NOT_FROM_A_SEARCH = "I read only a message a search found, or text you pasted."
BUSY_NOT_CALENDAR = "I look for free time only in your calendar."
SLOT_NOT_YOURS = "I book only a time you gave, or one your calendar showed free."
GUEST_NOT_YOURS = "I invite only an address you typed, or a confirmed contact."
NO_FREE_TIME = "There was no free time that long in that range."
IN_THE_PAST = "That time has already passed."
NOTHING_THERE = "There was nothing there to read."
DEPENDS = "It needed a step that did not run."
UNAVAILABLE = "I could not read that just now."


class Reader(Protocol):
    """The quarantined reader (D6): one message or the paste, one question."""

    def __call__(self, source: FoundMessage | PastedText, ask: Ask) -> object: ...


class CalendarReads(Protocol):
    def freebusy(self, start_utc: datetime, end_utc: datetime) -> list[BusyInterval]: ...

    def list_events(self, start_utc: datetime, end_utc: datetime) -> CalendarEvents: ...


GuestSource = Literal["typed", "contact"]
""""named by you" or "a confirmed contact", as the card says (D8)."""


@dataclass(frozen=True, slots=True)
class Guest:
    address: str
    source: GuestSource


@dataclass(frozen=True, slots=True)
class Proposal:
    """What a proposal step asks the owner to confirm."""

    step: int
    title: str
    start: datetime
    end: datetime
    timezone: str
    guests: tuple[Guest, ...]
    """None for a hold. The owner is never among them, under any address."""


@dataclass(frozen=True)
class Tools:
    gmail: MailReads
    calendar: CalendarReads
    contacts: Callable[[], Iterable[str]]
    """The confirmed contacts' guest keys, read once a turn."""
    read: Reader
    park: Callable[[Proposal], str]
    """Parks a proposal on the queue and returns its id (D8)."""
    hours: WorkingHours


@dataclass(frozen=True, slots=True)
class Turn:
    typed: str
    pasted: str | None
    owner: frozenset[str]
    """The owner's address and aliases."""
    zone: ZoneInfo
    """The owner's zone, which working hours are in."""


# --- what a step returns ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Found:
    messages: tuple[FoundMessage, ...]


@dataclass(frozen=True, slots=True)
class ReadAnswer:
    source: FoundMessage | PastedText
    answer: object


@dataclass(frozen=True, slots=True)
class Events:
    start: datetime
    end: datetime
    events: CalendarEvents


@dataclass(frozen=True, slots=True)
class Busy:
    start: datetime
    end: datetime
    busy: tuple[BusyInterval, ...]


@dataclass(frozen=True, slots=True)
class FreeSlot:
    slot: Slot | None
    """None when the range had no free time that long."""
    minutes: int


@dataclass(frozen=True, slots=True)
class Contact:
    address: str


@dataclass(frozen=True, slots=True)
class Parked:
    proposal_id: str
    proposal: Proposal


Result = Found | ReadAnswer | Events | Busy | FreeSlot | Contact | Parked


@dataclass(frozen=True, slots=True)
class Ran:
    step: int
    result: Result
    origin: Origin


@dataclass(frozen=True, slots=True)
class Refused:
    step: int
    reason: str


@dataclass(frozen=True, slots=True)
class Outcome:
    ran: tuple[Ran, ...]
    refused: tuple[Refused, ...]
    question: str | None
    """A step that needed the owner ended the turn with this."""
    pasted: tuple[str, ...]
    """Addresses in the pasted text, each offered for an Allow (D5)."""


class _RefusedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _NeedsOwnerError(Exception):
    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


def execute(plan: Plan, turn: Turn, tools: Tools, *, now: datetime) -> Outcome:
    """Run `plan`'s steps in order, each after D4's checks."""
    run = _Run(plan, turn, tools, now)
    ran: list[Ran] = []
    refused: list[Refused] = []
    question: str | None = None
    for number, step in enumerate(plan.steps, start=1):
        try:
            result, origin = run.step(number, step)
        except _RefusedError as refusal:
            refused.append(Refused(number, refusal.reason))
            continue
        except _NeedsOwnerError as asks:
            question = asks.question
            break
        except Exception as exc:
            # Gmail or the calendar failed. The type only: a message can quote mail.
            log.warning("step %d (%s) could not run: %s", number, step.op, type(exc).__name__)
            refused.append(Refused(number, UNAVAILABLE))
            continue
        run.done[number] = Ran(number, result, origin)
        ran.append(run.done[number])
    return Outcome(
        ran=tuple(ran),
        refused=tuple(refused),
        question=question,
        pasted=pasted_addresses(turn.pasted or "", exclude=turn.owner | run.typed),
    )


class _Run:
    def __init__(self, plan: Plan, turn: Turn, tools: Tools, now: datetime) -> None:
        self.plan = plan
        self.turn = turn
        self.tools = tools
        self.now = now
        self.done: dict[int, Ran] = {}
        self.owner = frozenset(guest_key(address) for address in turn.owner)
        self.typed = typed_addresses(turn.typed, turn.owner)

    @cached_property
    def contacts(self) -> frozenset[str]:
        return frozenset(self.tools.contacts())

    def step(self, number: int, step: Step) -> tuple[Result, Origin]:
        match step:
            case Search():
                return Found(search(self.tools.gmail, step)), computed(Origin.TYPED, Origin.MAIL)
            case Read():
                source, origin = self._source(step.message)
                answer = self.tools.read(source, step.ask)
                return ReadAnswer(source, answer), computed(origin, Origin.TYPED, Origin.MAIL)
            case ListEvents():
                events = self.tools.calendar.list_events(step.start, step.end)
                return Events(step.start, step.end, events), computed(Origin.TYPED, Origin.CALENDAR)
            case FreeBusy():
                busy = tuple(self.tools.calendar.freebusy(step.start, step.end))
                return Busy(step.start, step.end, busy), computed(Origin.TYPED, Origin.CALENDAR)
            case FirstFree():
                return self._first_free(step)
            case FindContact():
                match find_contact(step.name, self.contacts):
                    case Unclear(question):
                        raise _NeedsOwnerError(question)
                    case address:
                        return Contact(address), computed(Origin.TYPED, Origin.CONTACT)
            case ProposeHold() | ProposeInvite():
                return self._propose(number, step)

    def _needed(self, ref: Ref) -> Ran:
        ran = self.done.get(ref.step)
        if ran is None:
            raise _RefusedError(DEPENDS)
        return ran

    def _source(self, message: Ref | Pasted) -> tuple[FoundMessage | PastedText, Origin]:
        if isinstance(message, Pasted):
            if not self.turn.pasted:
                raise _RefusedError(NOTHING_THERE)
            return PastedText(self.turn.pasted), Origin.MAIL
        ran = self._needed(message)
        if not isinstance(ran.result, Found) or message.item is None:
            raise _RefusedError(NOT_FROM_A_SEARCH)
        if message.item >= len(ran.result.messages):
            raise _RefusedError(NOTHING_THERE)
        return ran.result.messages[message.item], ran.origin

    def _first_free(self, step: FirstFree) -> tuple[FreeSlot, Origin]:
        ran = self._needed(step.busy)
        if not isinstance(ran.result, Busy) or ran.origin is not Origin.CALENDAR:
            raise _RefusedError(BUSY_NOT_CALENDAR)
        busy = ran.result
        slot = first_free(
            busy.busy,
            busy.start,
            busy.end,
            step.minutes,
            hours=self.tools.hours,
            zone=self.turn.zone,
            now=self.now,
        )
        return FreeSlot(slot, step.minutes), computed(ran.origin, Origin.TYPED)

    def _propose(self, number: int, step: ProposeHold | ProposeInvite) -> tuple[Parked, Origin]:
        slot, slot_origin = self._slot(step.slot)
        if slot.start <= self.now:
            raise _RefusedError(IN_THE_PAST)
        guests, origins = self._guests(step) if isinstance(step, ProposeInvite) else ((), [])
        proposal = Proposal(
            step=number,
            title=step.title,
            start=slot.start,
            end=slot.end,
            timezone=self.plan.timezone,
            guests=guests,
        )
        return Parked(self.tools.park(proposal), proposal), computed(
            Origin.TYPED, slot_origin, *origins
        )

    def _slot(self, slot: Slot | Ref) -> tuple[Slot, Origin]:
        if isinstance(slot, Slot):
            return slot, Origin.TYPED
        ran = self._needed(slot)
        if not isinstance(ran.result, FreeSlot) or ran.origin > Origin.CALENDAR:
            raise _RefusedError(SLOT_NOT_YOURS)
        if ran.result.slot is None:
            raise _RefusedError(NO_FREE_TIME)
        return ran.result.slot, ran.origin

    def _guests(self, step: ProposeInvite) -> tuple[tuple[Guest, ...], list[Origin]]:
        """Each guest typed by the owner or a confirmed contact, the owner
        left out, one per mailbox."""
        chosen: dict[str, Guest] = {}
        origins: list[Origin] = []
        for written in step.guests:
            key = guest_key(written)
            if key in self.owner:
                continue
            if key in self.typed:
                guest, origin = Guest(written.strip().lower(), "typed"), Origin.TYPED
            elif key in self.contacts:
                guest, origin = Guest(written.strip().lower(), "contact"), Origin.CONTACT
            else:
                raise _RefusedError(GUEST_NOT_YOURS)
            chosen.setdefault(key, guest)
            origins.append(origin)
        for ref in step.guest_refs:
            ran = self._needed(ref)
            if not isinstance(ran.result, Contact) or ran.origin is not Origin.CONTACT:
                raise _RefusedError(GUEST_NOT_YOURS)
            key = guest_key(ran.result.address)
            if key not in self.owner:
                chosen.setdefault(key, Guest(ran.result.address, "contact"))
                origins.append(ran.origin)
        return tuple(chosen.values()), origins
