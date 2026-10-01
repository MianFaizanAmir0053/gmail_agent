"""Who is in the thread (M17, D4).

An invitation leaves the owner's account, so its guests are held to the
thread it came from. A guest may be invited when they are a participant, or
the owner allowed them once (`app/policy/contacts.py`). Everyone else is
outside, and a Confirm waits until the owner allows or removes them.

**Participants,** read from the thread's metadata, leaving out mail in SPAM
or TRASH:
- every recipient (`To`, `Cc`) of the owner's own mail. The label is
  Gmail's `SENT`, so a forged `From: owner` changes nothing;
- the sender of mail the owner received, but only when Gmail's own topmost
  `Authentication-Results` records `dmarc=pass` for the `From` address's
  domain. A sender writes their `From` as freely as their `Cc`; DMARC is the
  domain standing behind it, and only the topmost header is Gmail's.

Addresses only in the `To` or `Cc` of mail received are outside.

**The header is parsed, not split.** Parts of `Authentication-Results` are
the sender's own words -- an SPF comment, a quoted envelope address -- and
may contain `;` or `dmarc=pass`. Quoted strings and comments are skipped as
RFC 8601 defines them, and a header with more than one DMARC result is
refused outright.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from email.utils import getaddresses, parseaddr
from typing import Any

GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})

GMAIL_AUTHSERV = "mx.google.com"
"""The authserv-id of the `Authentication-Results` header Gmail writes."""

_LEFT_OUT = frozenset({"SPAM", "TRASH"})

SPACE = " \t\r\n\f\v\ufeff"
"""What is trimmed from an address before it is compared: ASCII space and a
byte-order mark. The web app trims the same set (`dashboard/src/lib/guests.ts`)."""


def guest_key(address: str) -> str:
    """One spelling per mailbox, for comparing guests.

    Lower case and exact, except for Gmail, which ignores dots and `+tags`
    and reads `googlemail.com` as `gmail.com`. Elsewhere a `+tag` or a dot
    can be a different mailbox, so it stays.
    """
    local, _, domain = address.strip(SPACE).lower().partition("@")
    if domain in GMAIL_DOMAINS:
        return f"{local.split('+', 1)[0].replace('.', '')}@gmail.com"
    return f"{local}@{domain}"


def participants(thread: Mapping[str, Any]) -> frozenset[str]:
    """The guest keys of everyone in a thread, from `threads.get` with
    `format=metadata`."""
    found: set[str] = set()
    for message in thread.get("messages") or []:
        labels = set(message.get("labelIds") or [])
        if labels & _LEFT_OUT:
            continue
        headers: Sequence[Mapping[str, str]] = (message.get("payload") or {}).get("headers") or []
        if "SENT" in labels:
            # One value at a time: the parser gives up on a whole call at the
            # first value it cannot read, such as `undisclosed-recipients:;`.
            for value in _values(headers, "To") + _values(headers, "Cc"):
                found.update(
                    guest_key(address) for _, address in getaddresses([value]) if "@" in address
                )
            continue
        sender = parseaddr(_first(headers, "From") or "")[1]
        if "@" in sender and dmarc_passed(headers, sender.rpartition("@")[2]):
            found.add(guest_key(sender))
    return frozenset(found)


def dmarc_passed(headers: Sequence[Mapping[str, str]], domain: str) -> bool:
    """Whether Gmail's own topmost `Authentication-Results` records
    `dmarc=pass` with `header.from` equal to `domain`."""
    topmost = _first(headers, "Authentication-Results")
    if topmost is None:
        return False
    server, *results = _statements(topmost)
    if not server or server[0].lower() != GMAIL_AUTHSERV:
        return False
    dmarc = [words for words in results if words and words[0].lower().startswith("dmarc=")]
    if len(dmarc) != 1:
        return False  # none, or one the sender wrote beside Gmail's
    method, *properties = dmarc[0]
    return method.lower() == "dmarc=pass" and f"header.from={domain.lower()}" in (
        prop.lower() for prop in properties
    )


def _statements(header: str) -> list[list[str]]:
    """`Authentication-Results` as RFC 8601 reads it: statements split on `;`,
    each split into words, with comments dropped and quoted strings kept as
    one word -- so neither can hide a `;` or a result of its own."""
    statements: list[list[str]] = [[]]
    word: list[str] = []
    depth = 0  # inside a comment, which may nest
    quoted = escaped = False

    def end_word() -> None:
        if word:
            statements[-1].append("".join(word))
            word.clear()

    for char in header:
        if escaped:
            escaped = False
            if quoted:
                word.append(char)
        elif char == "\\" and (quoted or depth):
            escaped = True
        elif quoted:
            if char == '"':
                quoted = False
            else:
                word.append(char)
        elif depth:
            depth += char == "("
            depth -= char == ")"
        elif char == "(":
            end_word()
            depth = 1
        elif char == '"':
            quoted = True
        elif char == ";":
            end_word()
            statements.append([])
        elif char.isspace():
            end_word()
        else:
            word.append(char)
    end_word()
    return statements


def outside(
    guests: Iterable[str], *, participants: frozenset[str], confirmed: frozenset[str]
) -> list[str]:
    """The guests who are neither participants nor confirmed contacts, as
    given, in order."""
    return [
        guest
        for guest in guests
        if guest_key(guest) not in participants and guest_key(guest) not in confirmed
    ]


def _values(headers: Sequence[Mapping[str, str]], name: str) -> list[str]:
    return [h.get("value", "") for h in headers if h.get("name", "").lower() == name.lower()]


def _first(headers: Sequence[Mapping[str, str]], name: str) -> str | None:
    values = _values(headers, name)
    return values[0] if values else None
