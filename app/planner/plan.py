"""The plan: what the planner may ask for, and the check before any of it runs (M19, D2).

The planner answers one structured call with a `PlanPayload`: a kind, at most
four steps and a reply. `to_plan` checks it and gives it types before anything
runs. A plan it refuses runs nothing, and the turn fails as "could not plan
this". Nothing of a plan's results ever goes back to a model.

## A flat schema

The endpoint takes a subset of JSON Schema, so the schema leans on little of it:
- a step is one object with every operation's fields, each optional in the
  schema and required by code (`FIELDS`);
- no field takes two kinds of value: a slot is `slot_ref` or `start` and
  `end`, and a guest is in `guests` (an address) or `guest_refs` (a contact an
  earlier step found);
- an earlier step's result is named, never copied: `$s1` is the first step's
  result and `$s1.0` its first item. `$paste` is the text the owner pasted,
  which only the reader reads (D5).

## Times

As in extraction, the model writes local wall-clock times and names the zone
they are in, once for the whole plan; `zoneinfo` does the arithmetic. A `Plan`
holds UTC instants. A search's `after` and `before` are dates, each taken as
the midnight that starts it in that zone.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import ClassVar, assert_never
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from app.extraction.llm import Completion, GenaiLike, structured_call
from app.policy.scrub import ADDRESS

MAX_STEPS = 4
MAX_READS = 3
"""The reader's calls in a turn (D6). Each `mail.read` step is one."""
MAX_RESULTS = 10
"""The most messages one search returns (D3)."""
MAX_TEXT = 200
"""The longest text a plan may hold: a title, a name, search words, a question."""
LONGEST_RANGE = timedelta(days=31)
"""The most a calendar read covers."""
LONGEST_SLOT = timedelta(hours=24)

PASTE = "$paste"
_REF = re.compile(r"\$s([0-9]{1,2})(?:\.([0-9]{1,2}))?")
_ADDRESS = re.compile(ADDRESS)

_RANGE_TOO_LONG = f"the range is longer than {LONGEST_RANGE.days} days"
_SLOT_TOO_LONG = f"the slot is longer than {LONGEST_SLOT // timedelta(hours=1)} hours"
_SLOT_MINUTES = LONGEST_SLOT // timedelta(minutes=1)


class Op(StrEnum):
    """The operation a step runs."""

    SEARCH = "mail.search"
    READ = "mail.read"
    LIST_EVENTS = "calendar.list_events"
    FREEBUSY = "calendar.freebusy"
    FIRST_FREE = "slots.first_free"
    FIND_CONTACT = "contacts.find"
    PROPOSE_HOLD = "calendar.propose_hold"
    PROPOSE_INVITE = "calendar.propose_invite"


class Kind(StrEnum):
    """answer: run the steps and show what they found. clarify: ask the owner
    a question instead. decline: refuse the request."""

    ANSWER = "answer"
    CLARIFY = "clarify"
    DECLINE = "decline"


class Template(StrEnum):
    """How an answer shows its steps' results (D7)."""

    FREE_BUSY = "free_busy"
    EVENTS = "events"
    MESSAGES = "messages"
    QUOTE = "quote"
    PROPOSAL = "proposal"


class Ask(StrEnum):
    """What the reader looks for in a message (D3)."""

    ASKS_OF_OWNER = "asks_of_owner"
    PEOPLE = "people"
    SUMMARY = "summary"


class Decline(StrEnum):
    """Why a request is refused, each with a fixed sentence (D7)."""

    CANNOT_SEND = "cannot_send"
    CANNOT_CHANGE = "cannot_change"
    EMAIL_SAYS = "email_says"
    OUT_OF_SCOPE = "out_of_scope"


FIELDS: dict[Op, frozenset[str]] = {
    Op.SEARCH: frozenset({"sender", "recipient", "words", "after", "before", "limit"}),
    Op.READ: frozenset({"message_ref", "ask"}),
    Op.LIST_EVENTS: frozenset({"start", "end"}),
    Op.FREEBUSY: frozenset({"start", "end"}),
    Op.FIRST_FREE: frozenset({"busy_ref", "minutes"}),
    Op.FIND_CONTACT: frozenset({"name"}),
    Op.PROPOSE_HOLD: frozenset({"title", "slot_ref", "start", "end"}),
    Op.PROPOSE_INVITE: frozenset({"title", "slot_ref", "start", "end", "guests", "guest_refs"}),
}
"""The fields each operation takes. The schema has all of them, each optional."""

RENDERS: dict[Template, tuple[Op, ...]] = {
    Template.FREE_BUSY: (Op.FREEBUSY, Op.FIRST_FREE),
    Template.EVENTS: (Op.LIST_EVENTS,),
    Template.MESSAGES: (Op.SEARCH,),
    Template.QUOTE: (Op.READ,),
    Template.PROPOSAL: (Op.PROPOSE_HOLD, Op.PROPOSE_INVITE),
}
"""The operations whose results each template shows."""

_FOR_RANGE_OR_SLOT = (
    "calendar.list_events and calendar.freebusy: the range. calendar.propose_hold and "
    "calendar.propose_invite: the slot, when there is no slot_ref. Local wall-clock "
    "ISO 8601 with no offset, e.g. '2026-10-08T15:00:00'."
)


# --- what the model writes ------------------------------------------------


class StepPayload(BaseModel):
    """One step. Fill only the fields its operation takes."""

    model_config = ConfigDict(extra="forbid")

    op: Op
    sender: str | SkipJsonSchema[None] = Field(
        default=None, description="mail.search: who sent it, a name or address the owner typed."
    )
    recipient: str | SkipJsonSchema[None] = Field(
        default=None,
        description="mail.search: who it was sent to, a name or address the owner typed.",
    )
    words: str | SkipJsonSchema[None] = Field(
        default=None, description="mail.search: plain words in the message, no search operators."
    )
    after: str | SkipJsonSchema[None] = Field(
        default=None, description="mail.search: sent on or after this date, YYYY-MM-DD."
    )
    before: str | SkipJsonSchema[None] = Field(
        default=None, description="mail.search: sent before this date, YYYY-MM-DD."
    )
    limit: int | SkipJsonSchema[None] = Field(
        default=None,
        description=f"mail.search: how many messages, newest first, 1 to {MAX_RESULTS}.",
    )
    message_ref: str | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "mail.read: $sN.I for message I (counting from 0) of search step N, or "
            f"{PASTE} for the text the owner pasted."
        ),
    )
    ask: Ask | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "mail.read: what to find. asks_of_owner: what the message asks the owner to "
            "do. people: who it names. summary: what it says."
        ),
    )
    start: str | SkipJsonSchema[None] = Field(default=None, description=_FOR_RANGE_OR_SLOT)
    end: str | SkipJsonSchema[None] = Field(default=None, description=_FOR_RANGE_OR_SLOT)
    busy_ref: str | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "slots.first_free: $sN, a calendar.freebusy step. Free time is looked for "
            "within that step's range."
        ),
    )
    minutes: int | SkipJsonSchema[None] = Field(
        default=None, description="slots.first_free: how long the free time must be."
    )
    name: str | SkipJsonSchema[None] = Field(
        default=None, description="contacts.find: the person's name, as the owner typed it."
    )
    title: str | SkipJsonSchema[None] = Field(
        default=None,
        description="calendar.propose_hold and calendar.propose_invite: the event's title.",
    )
    slot_ref: str | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "calendar.propose_hold and calendar.propose_invite: $sN, a slots.first_free "
            "step, instead of start and end."
        ),
    )
    guests: list[str] | SkipJsonSchema[None] = Field(
        default=None, description="calendar.propose_invite: email addresses the owner typed."
    )
    guest_refs: list[str] | SkipJsonSchema[None] = Field(
        default=None,
        description="calendar.propose_invite: $sN for each contacts.find step to invite.",
    )


class ReplyPayload(BaseModel):
    """What the owner is told. Fill only the field for the plan's kind."""

    model_config = ConfigDict(extra="forbid")

    template: Template | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "answer: how the results are shown. free_busy: free or busy, or a free slot. "
            "events: the events in a range. messages: what a search found. quote: what "
            "a read found. proposal: an event for the owner to confirm."
        ),
    )
    question: str | SkipJsonSchema[None] = Field(
        default=None,
        description="clarify: the question for the owner, made from their request alone.",
    )
    decline_code: Decline | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "decline: why. cannot_send: sending, replying or forwarding mail. "
            "cannot_change: deleting, archiving or labelling mail, or changing or "
            "cancelling an event. email_says: doing what an email or pasted text asks. "
            "out_of_scope: anything else."
        ),
    )


class PlanPayload(BaseModel):
    """The plan for the owner's request. Code runs it, and you never see its results."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    timezone: str = Field(
        description=(
            "The IANA zone every time in this plan is in, e.g. 'Asia/Karachi'. "
            "Never an offset like '+05:00'."
        )
    )
    steps: list[StepPayload] = Field(
        max_length=MAX_STEPS,
        description=(
            f"At most {MAX_STEPS}, run in order; none unless kind is answer. A step names "
            "an earlier step's result as $sN, N counting from 1."
        ),
    )
    reply: ReplyPayload


# --- what code runs ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ref:
    """An earlier step's result, or one item of it. `step` counts from 1."""

    step: int
    item: int | None = None


@dataclass(frozen=True, slots=True)
class Pasted:
    """The text the owner pasted with the request (D5)."""


@dataclass(frozen=True, slots=True)
class Slot:
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class Search:
    op: ClassVar[Op] = Op.SEARCH
    sender: str | None
    recipient: str | None
    words: str | None
    after: datetime | None
    before: datetime | None
    limit: int


@dataclass(frozen=True, slots=True)
class Read:
    op: ClassVar[Op] = Op.READ
    message: Ref | Pasted
    ask: Ask


@dataclass(frozen=True, slots=True)
class ListEvents:
    op: ClassVar[Op] = Op.LIST_EVENTS
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class FreeBusy:
    op: ClassVar[Op] = Op.FREEBUSY
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class FirstFree:
    """The first free time of `minutes` in a free/busy step's range."""

    op: ClassVar[Op] = Op.FIRST_FREE
    busy: Ref
    minutes: int


@dataclass(frozen=True, slots=True)
class FindContact:
    op: ClassVar[Op] = Op.FIND_CONTACT
    name: str


@dataclass(frozen=True, slots=True)
class ProposeHold:
    op: ClassVar[Op] = Op.PROPOSE_HOLD
    title: str
    slot: Slot | Ref


@dataclass(frozen=True, slots=True)
class ProposeInvite:
    """`guests` are addresses as the plan wrote them. Whether the owner typed
    each one is the executor's check (D4, D5), not this module's."""

    op: ClassVar[Op] = Op.PROPOSE_INVITE
    title: str
    slot: Slot | Ref
    guests: tuple[str, ...]
    guest_refs: tuple[Ref, ...]


Step = Search | Read | ListEvents | FreeBusy | FirstFree | FindContact | ProposeHold | ProposeInvite


@dataclass(frozen=True, slots=True)
class Plan:
    kind: Kind
    steps: tuple[Step, ...]
    template: Template | None
    question: str | None
    decline: Decline | None
    timezone: str


class InvalidPlanError(ValueError):
    """The plan cannot run. The message names the step, the field and the
    problem, never a value: a stored error quotes nothing (M18, D7)."""


PLAN_THINKING = "LOW"
PLAN_OUTPUT_TOKENS = 2048


def make_plan(
    client: GenaiLike,
    *,
    model: str,
    typed: str,
    pasted: bool,
    asked_at: datetime,
    zone: str,
) -> Completion[PlanPayload]:
    """The planner's one call: the owner's typed request in, a plan out, at a
    low thinking level and the model's own temperature. `pasted` tells the
    planner only that there is pasted text, never what it says (D5)."""
    from app.planner.prompts import PLAN_SYSTEM, plan_user

    return structured_call(
        client,
        model=model,
        system=PLAN_SYSTEM,
        user=plan_user(typed, pasted=pasted, asked_at=asked_at, zone=zone),
        schema=PlanPayload,
        thinking_level=PLAN_THINKING,
        max_output_tokens=PLAN_OUTPUT_TOKENS,
    )


# --- the check --------------------------------------------------------------


def to_plan(payload: PlanPayload, *, pasted: bool) -> Plan:
    """Check a plan and give it types, or refuse all of it.

    `pasted` says whether the owner pasted text the plan may read. The schema
    has already refused an unknown operation, an unknown field and a fifth
    step; this refuses the rest of D2's list.
    """
    zone = _zone(payload.timezone)
    steps: list[Step] = []
    for number, written in enumerate(payload.steps, start=1):
        at = _At(
            step=number, before=tuple(steps), count=len(payload.steps), zone=zone, pasted=pasted
        )
        steps.append(_step(written, at))

    ops = [step.op for step in steps]
    reply = payload.reply
    question = _text(reply.question, "reply.question", InvalidPlanError)
    _fits_kind(
        payload.kind,
        ops,
        {"template": reply.template, "question": question, "decline_code": reply.decline_code},
    )
    if reply.template is not None:
        _shows_something(reply.template, ops)
    if ops.count(Op.READ) > MAX_READS:
        raise InvalidPlanError(f"a plan makes at most {MAX_READS} reads")
    return Plan(
        kind=payload.kind,
        steps=tuple(steps),
        template=reply.template,
        question=question,
        decline=reply.decline_code,
        timezone=payload.timezone,
    )


@dataclass(frozen=True, slots=True)
class _At:
    """Where a check is: the step, the steps checked before it, and the plan."""

    step: int
    before: tuple[Step, ...]
    count: int
    zone: ZoneInfo
    pasted: bool

    def refuse(self, problem: str) -> InvalidPlanError:
        return InvalidPlanError(f"step {self.step}: {problem}")


_STEP_FIELDS = tuple(name for name in StepPayload.model_fields if name != "op")


def _step(step: StepPayload, at: _At) -> Step:
    op = step.op
    taken = FIELDS[op]
    stray = [name for name in _STEP_FIELDS if name not in taken and _given(getattr(step, name))]
    if stray:
        verb = "is" if len(stray) == 1 else "are"
        raise at.refuse(f"{' and '.join(stray)} {verb} not taken by {op}")

    match op:
        case Op.SEARCH:
            return _search(step, at)
        case Op.READ:
            message = _message(step.message_ref, at)
            return Read(message=message, ask=_need(step.ask, "ask", at))
        case Op.LIST_EVENTS:
            return ListEvents(*_range(step, LONGEST_RANGE, _RANGE_TOO_LONG, at))
        case Op.FREEBUSY:
            return FreeBusy(*_range(step, LONGEST_RANGE, _RANGE_TOO_LONG, at))
        case Op.FIRST_FREE:
            busy_ref = _need(_squeeze(step.busy_ref), "busy_ref", at)
            busy = _whole(busy_ref, "busy_ref", Op.FREEBUSY, at)
            minutes = _need(step.minutes, "minutes", at)
            if not 1 <= minutes <= _SLOT_MINUTES:
                raise at.refuse(f"minutes is not between 1 and {_SLOT_MINUTES}")
            return FirstFree(busy=busy, minutes=minutes)
        case Op.FIND_CONTACT:
            return FindContact(name=_need(_text(step.name, "name", at.refuse), "name", at))
        case Op.PROPOSE_HOLD:
            return ProposeHold(title=_title(step, at), slot=_slot(step, at))
        case Op.PROPOSE_INVITE:
            title, slot = _title(step, at), _slot(step, at)
            guests = _guests(step.guests, at)
            refs = tuple(
                _whole(ref, "guest_refs", Op.FIND_CONTACT, at) for ref in _squeezed(step.guest_refs)
            )
            if not guests and not refs:
                raise at.refuse("guests is missing")
            return ProposeInvite(title=title, slot=slot, guests=guests, guest_refs=refs)
        case _:
            assert_never(op)


def _search(step: StepPayload, at: _At) -> Search:
    limit = _need(step.limit, "limit", at)
    if not 1 <= limit <= MAX_RESULTS:
        raise at.refuse(f"limit is not between 1 and {MAX_RESULTS}")
    after = _midnight(step.after, "after", at)
    before = _midnight(step.before, "before", at)
    if after is not None and before is not None and after >= before:
        raise at.refuse("after is not earlier than before")
    return Search(
        sender=_text(step.sender, "sender", at.refuse),
        recipient=_text(step.recipient, "recipient", at.refuse),
        words=_text(step.words, "words", at.refuse),
        after=after,
        before=before,
        limit=limit,
    )


def _message(value: str | None, at: _At) -> Ref | Pasted:
    text = _need(_squeeze(value), "message_ref", at)
    if text == PASTE:
        if not at.pasted:
            raise at.refuse("message_ref points to no pasted text")
        return Pasted()
    ref = _ref(text, "message_ref", at)
    source = at.before[ref.step - 1]
    if not isinstance(source, Search):
        raise at.refuse(f"message_ref names a {source.op} step, not {Op.SEARCH}")
    if ref.item is None:
        raise at.refuse("message_ref names a list where one value is wanted")
    if ref.item >= source.limit:
        raise at.refuse(f"message_ref points past what step {ref.step} returns")
    return ref


def _whole(value: str, field: str, want: Op, at: _At) -> Ref:
    """A reference to the whole result of a `want` step."""
    ref = _ref(value, field, at)
    source = at.before[ref.step - 1]
    if source.op is not want:
        raise at.refuse(f"{field} names a {source.op} step, not {want}")
    if ref.item is not None:
        raise at.refuse(f"{field} names an item of a result that is not a list")
    return ref


def _ref(value: str, field: str, at: _At) -> Ref:
    match = _REF.fullmatch(value)
    if match is None:
        raise at.refuse(f"{field} is not a reference")
    step = int(match[1])
    if not 1 <= step <= at.count:
        raise at.refuse(f"{field} points to no step")
    if step >= at.step:
        raise at.refuse(f"{field} points forward")
    return Ref(step=step, item=None if match[2] is None else int(match[2]))


def _slot(step: StepPayload, at: _At) -> Slot | Ref:
    ref = _squeeze(step.slot_ref)
    if ref is None:
        return Slot(*_range(step, LONGEST_SLOT, _SLOT_TOO_LONG, at))
    if _given(step.start) or _given(step.end):
        raise at.refuse("slot_ref is given with start or end")
    return _whole(ref, "slot_ref", Op.FIRST_FREE, at)


def _title(step: StepPayload, at: _At) -> str:
    return _need(_text(step.title, "title", at.refuse), "title", at)


def _guests(values: list[str] | None, at: _At) -> tuple[str, ...]:
    addresses = [value.lower() for value in _squeezed(values)]
    if any(_ADDRESS.fullmatch(address) is None for address in addresses):
        raise at.refuse("guests holds a non-address")
    return tuple(dict.fromkeys(addresses))


def _range(
    step: StepPayload, longest: timedelta, too_long: str, at: _At
) -> tuple[datetime, datetime]:
    start = _instant(step.start, "start", at)
    end = _instant(step.end, "end", at)
    if end <= start:
        raise at.refuse("end is not after start")
    if end - start > longest:
        raise at.refuse(too_long)
    return start, end


def _instant(value: str | None, field: str, at: _At) -> datetime:
    text = _need(_squeeze(value), field, at)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise at.refuse(f"{field} is not a local time") from None
    # As in extraction: an offset the model wrote anyway is the one it meant.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=at.zone)
    return moment.astimezone(UTC)


def _midnight(value: str | None, field: str, at: _At) -> datetime | None:
    text = _squeeze(value)
    if text is None:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise at.refuse(f"{field} is not a date") from None
    return datetime.combine(day, time(), tzinfo=at.zone).astimezone(UTC)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidPlanError("timezone is not a known zone") from None


def _fits_kind(kind: Kind, ops: Sequence[Op], reply: dict[str, object]) -> None:
    """An answer runs steps and names a template; a clarify asks a question; a
    decline gives a code. Each runs nothing and fills nothing else."""
    a_plan = f"{'an' if kind is Kind.ANSWER else 'a'} {kind} plan"
    if kind is Kind.ANSWER and not ops:
        raise InvalidPlanError(f"{a_plan} runs at least one step")
    if kind is not Kind.ANSWER and ops:
        raise InvalidPlanError(f"{a_plan} runs no steps")
    taken = {Kind.ANSWER: "template", Kind.CLARIFY: "question", Kind.DECLINE: "decline_code"}
    for field, value in reply.items():
        if field == taken[kind] and value is None:
            raise InvalidPlanError(f"reply.{field} is missing")
        if field != taken[kind] and value is not None:
            raise InvalidPlanError(f"reply.{field} is not taken by {a_plan}")


def _shows_something(template: Template, ops: Sequence[Op]) -> None:
    """A template shows a step's results, and a proposal is always said to be one."""
    if any(op in RENDERS[Template.PROPOSAL] for op in ops) and template is not Template.PROPOSAL:
        raise InvalidPlanError(f"a proposal is answered with reply.template {Template.PROPOSAL}")
    if not set(RENDERS[template]) & set(ops):
        shown = " or ".join(RENDERS[template])
        raise InvalidPlanError(f"reply.template {template} has no {shown} step to show")


# --- values ----------------------------------------------------------------


def _squeeze(value: str | None) -> str | None:
    """Whitespace collapsed. A blank value is an absent one: a model fills an
    unused field with "" as readily as it leaves it out."""
    if value is None:
        return None
    return " ".join(value.split()) or None


def _squeezed(values: list[str] | None) -> list[str]:
    return [text for value in values or () if (text := _squeeze(value)) is not None]


def _given(value: object) -> bool:
    if isinstance(value, str):
        return _squeeze(value) is not None
    if isinstance(value, list):
        return bool(_squeezed(value))
    return value is not None


def _text(value: str | None, field: str, refuse: Callable[[str], Exception]) -> str | None:
    text = _squeeze(value)
    if text is not None and len(text) > MAX_TEXT:
        raise refuse(f"{field} is longer than {MAX_TEXT} characters")
    return text


def _need[V](value: V | None, field: str, at: _At) -> V:
    if value is None:
        raise at.refuse(f"{field} is missing")
    return value
