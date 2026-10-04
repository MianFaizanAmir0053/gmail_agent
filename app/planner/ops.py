"""The planner's reads and helpers (M19, D3), run by code, never by a model.

- `search`: Gmail's own search. The query is built here from the plan's typed
  fields (`search_query`); the planner never writes Gmail syntax.
- `first_free`: the earliest free time of a length, in working hours.
- `find_contact`: a name matched against the confirmed contacts, and nothing
  else. Mail is never searched for an address.

The calendar's own reads are the client's, on the primary calendar
(`CalendarClient.freebusy` and `list_events`). What comes back here has no
origin yet: the executor gives each value one (D4).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Protocol
from zoneinfo import ZoneInfo

from app.config import WorkingHours
from app.google.calendar import BusyInterval
from app.google.gmail import BINNED, SEARCH_HEADERS, MessageGoneError, MessageMeta, epoch_window
from app.planner.plan import Search, Slot
from app.policy.participants import dmarc_passed
from app.policy.scrub import ADDRESS, redact_secrets
from app.tools.calendar_tool import overlaps

SEARCH_LEAVES_OUT = "-in:drafts -in:chats"
"""Unsent drafts and chats are not mail anyone sent: every search leaves them
out, as the mail sync's own queries do (`app/mail/messages.py`)."""

_ADDRESS = re.compile(ADDRESS)

SLOT_STEP = timedelta(minutes=15)
"""A free slot starts on a quarter hour, in the owner's zone."""

NO_CONTACT = "No confirmed contact has that name. Type their email address."
SEVERAL_CONTACTS = "More than one confirmed contact has that name. Type the address you mean."


@dataclass(frozen=True, slots=True)
class FoundMessage:
    """A message a search found, as its metadata says. Everything but `id` and
    `thread_id` is shown to the owner only, labelled as mail's (D7)."""

    id: str
    thread_id: str
    sender: str
    """The `From` address, lower-cased. Never the display name, which a
    sender writes as freely as the rest."""
    dmarc_passed: bool
    """Gmail's own topmost `Authentication-Results` passes DMARC for the
    sender's domain (`participants.dmarc_passed`)."""
    sent: bool
    """The owner sent it: Gmail's `SENT` label, which no sender can set."""
    received: datetime
    """Gmail's `internalDate`, never the sender's `Date` header."""
    subject: str
    """With every code and link removed: a metadata read never sees the body,
    so it cannot tell credential mail, and treats every subject as one."""


class MailReads(Protocol):
    """What a search needs of Gmail: `GmailClient`, or the eval's fake."""

    def search(self, query: str, limit: int = 200) -> list[str]: ...

    def message_metadata(self, message_id: str, headers: tuple[str, ...] = ...) -> MessageMeta: ...


@dataclass(frozen=True, slots=True)
class Unclear:
    """A step that cannot go on without the owner. The turn ends by asking
    this fixed question (D3)."""

    question: str


# --- mail.search ------------------------------------------------------------


def search_query(step: Search) -> str:
    """Gmail's query for a search step.

    Each value the planner wrote goes inside quote marks, with any quote mark
    of its own removed, so Gmail reads all of it as text and none of it as an
    operator. The only operators are the ones written here.
    """
    terms: list[str | None] = []
    if step.sender:
        terms.append(_operator("from", step.sender))
    if step.recipient:
        terms.append(_operator("to", step.recipient))
    if step.words:
        terms.extend(_quoted(word) for word in step.words.split())
    terms.append(SEARCH_LEAVES_OUT)
    terms.append(epoch_window(step.after, step.before) or None)
    return " ".join(term for term in terms if term)


def _operator(name: str, value: str) -> str | None:
    quoted = _quoted(value)
    return f"{name}:{quoted}" if quoted else None


def _quoted(value: str) -> str | None:
    text = " ".join(value.replace('"', " ").split())
    return f'"{text}"' if text else None


def search(gmail: MailReads, step: Search) -> tuple[FoundMessage, ...]:
    """The newest messages matching the step, at most its limit. Spam and
    trash are never listed (Gmail's default); a message that left the mailbox
    between the listing and the read is left out."""
    found: list[FoundMessage] = []
    for message_id in gmail.search(search_query(step), limit=step.limit):
        try:
            meta = gmail.message_metadata(message_id, SEARCH_HEADERS)
        except MessageGoneError:
            continue
        if meta.label_ids & BINNED:
            continue
        # A plain address or none: a quoted local part can spell another address.
        sender = parseaddr(meta.headers.get("From", ""))[1].strip().lower()
        if not _ADDRESS.fullmatch(sender):
            sender = ""
        domain = sender.rpartition("@")[2]
        headers = [{"name": name, "value": value} for name, value in meta.headers.items()]
        found.append(
            FoundMessage(
                id=meta.id,
                thread_id=meta.thread_id,
                sender=sender,
                dmarc_passed=bool(domain) and dmarc_passed(headers, domain),
                sent="SENT" in meta.label_ids,
                received=meta.internal_date,
                subject=redact_secrets(meta.headers.get("Subject", "")),
            )
        )
    return tuple(found)


# --- slots.first_free -------------------------------------------------------


def first_free(
    busy: Sequence[BusyInterval],
    start: datetime,
    end: datetime,
    minutes: int,
    *,
    hours: WorkingHours,
    zone: ZoneInfo,
    now: datetime,
) -> Slot | None:
    """The earliest `minutes` of free time between `start` and `end`, inside
    working hours in `zone`, starting on a quarter hour and never before `now`.
    None when there is none.

    `start` and `end` are the range of the free/busy read that `busy` came
    from: time outside it was never read, so it is never offered.
    """
    length = timedelta(minutes=minutes)
    earliest = max(start, now)
    day = earliest.astimezone(zone).date()
    while day <= end.astimezone(zone).date():
        if day.weekday() in hours.days:
            opens = datetime.combine(day, hours.start, tzinfo=zone)
            closes = min(end, datetime.combine(day, hours.end, tzinfo=zone))
            candidate = _on_a_quarter(max(earliest, opens), zone)
            while candidate + length <= closes:
                clashes = overlaps(candidate, candidate + length, list(busy))
                if not clashes:
                    return Slot(start=candidate, end=candidate + length)
                candidate = _on_a_quarter(max(clash.end for clash in clashes), zone)
        day += timedelta(days=1)
    return None


def _on_a_quarter(moment: datetime, zone: ZoneInfo) -> datetime:
    """`moment`, or the first quarter hour after it, in `zone`; in UTC."""
    local = moment.astimezone(zone)
    floor = local.replace(minute=local.minute - local.minute % 15, second=0, microsecond=0)
    return (floor if floor == local else floor + SLOT_STEP).astimezone(UTC)


# --- contacts.find ----------------------------------------------------------


def find_contact(name: str, contacts: Iterable[str]) -> str | Unclear:
    """The one confirmed contact `name` names, or the question to ask.

    A name names an address when each of its words is a word of the part
    before the at-sign, or its letters are all of them: "Sara" names
    sara.khan@, and "Bilal Ahmed" names bilalahmed@, but "Sar" names neither.
    Case and punctuation are ignored. `contacts` are the confirmed contacts
    and nothing else (D3).
    """
    wanted = _words(name)
    if not wanted:
        return Unclear(NO_CONTACT)
    matches = sorted({contact for contact in contacts if _names(wanted, contact)})
    if len(matches) == 1:
        return matches[0]
    return Unclear(SEVERAL_CONTACTS if matches else NO_CONTACT)


def _names(wanted: list[str], address: str) -> bool:
    words = _words(address.rpartition("@")[0])
    return set(wanted) <= set(words) or "".join(wanted) == "".join(words)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower())
