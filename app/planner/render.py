"""Answers, rendered by code (M19, D7). No model writes what the owner is told.

A plan's reply names one template, which shows its steps' results:
- `free_busy`: free or busy at a time, or the first free slot;
- `events`: the events in a range;
- `messages`: the messages a search found;
- `quote`: what the reader found in a message, or in the paste;
- `proposal`: a card made for the owner's Confirm.

**Labels.** Every value from mail carries who sent it -- the address, never a
display name -- whether Gmail verified their domain, and when it came: each
message in a list, and each quote. A name the reader gives is shown only as
what the message wrote. An event a stranger organised shows its title in
quotation marks, under its organiser, since a stranger's invitation reaches
the calendar by default. Calendar answers name the calendar (decision 11).

**Form.** Plain text: no Markdown, and no link made from text. Text from mail
or from an event passes through `shown`, so links and codes are gone and
each is one line. A step that did not run says why, and the addresses in a
paste are listed, each for the owner's Allow (D5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.google.calendar import CalendarEvent
from app.planner.execute import (
    Busy,
    Events,
    Found,
    FreeSlot,
    Outcome,
    Parked,
    ReadAnswer,
    Result,
)
from app.planner.ops import FoundMessage
from app.planner.origins import FromMessage, FromPaste
from app.planner.plan import Decline, Kind, Plan, Template
from app.planner.reader import Person, Reading
from app.policy.scrub import shown

CALENDAR = "your primary calendar"

DECLINES: dict[Decline, str] = {
    Decline.CANNOT_SEND: "Sending, replying to or forwarding email is not something I can do.",
    Decline.CANNOT_CHANGE: ("I can't delete, archive or label mail, or change or cancel an event."),
    Decline.EMAIL_SAYS: "I don't act on what an email asks. I can tell you what it says.",
    Decline.OUT_OF_SCOPE: (
        "That's not something I can do. I answer questions about your mail and "
        "calendar, and make cards for events you confirm."
    ),
}

COULD_NOT_PLAN = "I could not work out how to do that. Try asking another way."
NOTHING_TO_SHOW = "There was nothing to show."

GUEST_WORDS = {"typed": "named by you", "contact": "a confirmed contact"}
"""How a card's guests were named (D8), as the answer says it."""

_FOUND_IN_MESSAGE = {
    "email": "in the email",
    "quoted": "only in a quoted or forwarded section",
    "absent": "not found in the email",
}
_FOUND_IN_PASTE = {
    "email": "in the text you pasted",
    "quoted": "in the text you pasted",
    "absent": "not found in the text you pasted",
}


@dataclass(frozen=True, slots=True)
class Answer:
    text: str
    """Plain text, for the owner."""
    allow: tuple[str, ...]
    """Addresses from the paste, each offered for an Allow."""
    cards: tuple[str, ...]
    """The proposals parked, by id."""


def render(plan: Plan, outcome: Outcome, *, zone: ZoneInfo) -> Answer:
    """The answer to a turn: the reply its plan names, what would not run and
    why, and the paste's addresses."""
    lines: list[str] = []
    if plan.kind is Kind.DECLINE and plan.decline is not None:
        lines.append(DECLINES[plan.decline])
    elif plan.kind is Kind.CLARIFY:
        lines.append(shown(plan.question) or COULD_NOT_PLAN)
    elif outcome.question is not None:
        lines.append(outcome.question)
    elif plan.template is not None:
        lines.extend(_shown_by(plan.template, [ran.result for ran in outcome.ran], zone))
    lines.extend(f"Not done: {refused.reason}" for refused in outcome.refused)
    if outcome.pasted:
        lines.append(
            "Addresses in the text you pasted, each needing your Allow before it can be "
            "invited: " + ", ".join(outcome.pasted)
        )
    cards = tuple(ran.result.proposal_id for ran in outcome.ran if isinstance(ran.result, Parked))
    return Answer("\n".join(lines) or NOTHING_TO_SHOW, outcome.pasted, cards)


def _shown_by(template: Template, results: list[Result], zone: ZoneInfo) -> list[str]:
    match template:
        case Template.FREE_BUSY:
            slots = [result for result in results if isinstance(result, FreeSlot)]
            if slots:
                return [_slot(slot, zone) for slot in slots]
            return [_busy(result, zone) for result in results if isinstance(result, Busy)]
        case Template.EVENTS:
            return [
                line
                for result in results
                if isinstance(result, Events)
                for line in _events(result, zone)
            ]
        case Template.MESSAGES:
            return [
                line
                for result in results
                if isinstance(result, Found)
                for line in _messages(result, zone)
            ]
        case Template.QUOTE:
            return [
                line
                for result in results
                if isinstance(result, ReadAnswer)
                for line in _reading(result, zone)
            ]
        case Template.PROPOSAL:
            return [_card(result, zone) for result in results if isinstance(result, Parked)]


# --- the calendar -------------------------------------------------------------------


def _busy(result: Busy, zone: ZoneInfo) -> str:
    when = f"On {CALENDAR}, {_span(result.start, result.end, zone)}"
    if not result.busy:
        return f"{when}: you're free."
    busy = ", ".join(
        _span(interval.start, interval.end, zone, short=True) for interval in result.busy
    )
    return f"{when}: you're busy {busy}."


def _slot(result: FreeSlot, zone: ZoneInfo) -> str:
    if result.slot is None:
        return f"No free {result.minutes} minutes on {CALENDAR} then, within working hours."
    return (
        f"The first free {result.minutes} minutes on {CALENDAR}: "
        f"{_span(result.slot.start, result.slot.end, zone)}."
    )


def _events(result: Events, zone: ZoneInfo) -> list[str]:
    heading = f"On {CALENDAR}, {_span(result.start, result.end, zone)}:"
    events = result.events.events
    if not events:
        return [f"{heading} nothing."]
    lines = [heading, *(f"- {_event(event, zone)}" for event in events)]
    if result.events.more:
        lines.append("- and more, which I did not read.")
    return lines


def _event(event: CalendarEvent, zone: ZoneInfo) -> str:
    when = "all day" if event.all_day else _span(event.start, event.end, zone)
    title = shown(event.title) or "(no title)"
    if event.by_owner:
        return f"{when} {title}"
    # A stranger's invitation reaches the calendar by default (D7).
    organizer = event.organizer or "an unknown organiser"
    return f"{when} {_quoted(title)}, an invitation from {organizer}"


# --- mail ---------------------------------------------------------------------------


def _messages(result: Found, zone: ZoneInfo) -> list[str]:
    if not result.messages:
        return ["No messages matched."]
    count = len(result.messages)
    lines = [f"{count} message{'s' if count != 1 else ''} in your mail:"]
    for message in result.messages:
        subject = shown(message.subject) or "(no subject)"
        lines.append(f"- {_quoted(subject)}, {_from(_label(message), zone)}")
    return lines


def _label(message: FoundMessage) -> FromMessage:
    return FromMessage(message.sender, message.dmarc_passed, message.received, message.sent)


def _from(label: FromMessage | FromPaste, zone: ZoneInfo) -> str:
    """Who a value from mail came from: an address, Gmail's verdict, a date."""
    if isinstance(label, FromPaste):
        return "from the text you pasted"
    when = _moment(label.received, zone)
    if label.sent:
        return f"sent by you, {when}"
    verdict = "domain verified by Gmail" if label.dmarc_passed else "domain not verified"
    return f"from {label.sender or 'an unknown sender'} ({verdict}), {when}"


def _reading(result: ReadAnswer, zone: ZoneInfo) -> list[str]:
    reading = result.answer
    if not isinstance(reading, Reading):
        return [NOTHING_TO_SHOW]
    heading = _from(reading.label, zone)
    heading = heading[0].upper() + heading[1:]
    if reading.unread is not None:
        return [f"{heading}: {reading.unread}"]
    if not reading.points and not reading.people:
        return [f"{heading}: nothing I could report."]
    lines = [f"{heading}:"]
    lines.extend(f"- {_quoted(point.text)}" for point in reading.points)
    if reading.people:
        lines.append("People it names:")
        lines.extend(f"- {_person(person, reading.label)}" for person in reading.people)
    return lines


def _person(person: Person, label: FromMessage | FromPaste) -> str:
    """An address, with what the message called it: a name is never who someone is."""
    written = f"written as {_quoted(person.name.text)}" if person.name else "with no name"
    if person.address is None:
        return f"someone {written}, with no address"
    words = _FOUND_IN_PASTE if isinstance(label, FromPaste) else _FOUND_IN_MESSAGE
    found = f" ({words[person.found]})" if person.found else ""
    return f"{person.address}, {written}{found}"


# --- cards --------------------------------------------------------------------------


def _card(result: Parked, zone: ZoneInfo) -> str:
    proposal = result.proposal
    title = shown(proposal.title) or "(no title)"
    when = _span(proposal.start, proposal.end, zone)
    if not proposal.guests:
        whom = f"on {CALENDAR} only"
    else:
        whom = "inviting " + ", ".join(
            f"{guest.address} ({GUEST_WORDS[guest.source]})" for guest in proposal.guests
        )
    return f"A card is waiting for your Confirm: {title}, {when}, {whom}."


# --- words --------------------------------------------------------------------------


def _quoted(text: str) -> str:
    return f"“{text}”"


def _moment(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{local:%a} {local.day} {local:%b} {local:%H:%M}"


def _span(start: datetime, end: datetime, zone: ZoneInfo, *, short: bool = False) -> str:
    """`Thu 8 Oct 15:00-16:00`, or just the times when `short`; across days,
    both ends in full."""
    first, last = start.astimezone(zone), end.astimezone(zone)
    if short and first.date() == last.date():
        return f"{first:%H:%M}-{last:%H:%M}"
    if first.date() == last.date():
        return f"{_moment(start, zone)}-{last:%H:%M}"
    return f"{_moment(start, zone)} to {_moment(end, zone)}"
