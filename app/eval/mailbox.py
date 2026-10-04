"""Gmail's search, as far as the agent's own queries go, over plain records.

The mail sync's fake mailbox (`tests/fake_gmail.py`) and the planner's
routing eval (`app/eval/planner.py`) both read a query here, so the two read
it the same way. A term this does not know fails, rather than matching
everything: a query nobody meant to write is a bug in whatever wrote it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from datetime import datetime

IN_LABELS = {"chats": "CHAT", "drafts": "DRAFT", "sent": "SENT", "inbox": "INBOX"}

_TERM = re.compile(r'(-?)(?:(\w+):"([^"]*)"|"([^"]*)"|(\w+):(\S+)|(\S+))')
"""`key:"a value"`, a quoted `"word"`, `key:value`, or anything else, which
is refused: an unquoted word in a query is a bug in what built it."""

WORD_HEADERS = ("Subject", "From", "To")
"""Where a quoted word is looked for. Gmail also reads the body; the cases
are written so these are enough."""


@dataclass(frozen=True, slots=True)
class Term:
    negate: bool
    key: str
    """Empty for a quoted word, which Gmail reads as text, never as an operator."""
    value: str


def terms(query: str) -> list[Term]:
    found: list[Term] = []
    for match in _TERM.finditer(query):
        negate = match[1] == "-"
        if match[2] is not None:
            found.append(Term(negate, match[2], match[3]))
        elif match[4] is not None:
            found.append(Term(negate, "", match[4]))
        elif match[5] is not None:
            found.append(Term(negate, match[5], match[6]))
        else:
            raise AssertionError(f"the fake mailbox does not understand {match[7]!r}")
    return found


def matches(
    terms: list[Term], *, labels: Set[str], at: datetime, headers: Mapping[str, str]
) -> bool:
    """Whether a message with these labels, internal date and headers matches."""
    # Gmail's default never lists spam or trash; `in:anywhere` does.
    anywhere = any((term.key, term.value) == ("in", "anywhere") for term in terms)
    if labels & {"SPAM", "TRASH"} and not anywhere:
        return False
    seconds = at.timestamp()
    for term in terms:
        key, value = term.key, term.value
        if (key, value) == ("in", "anywhere"):
            found = True
        elif key == "rfc822msgid":
            found = headers.get("Message-ID", "").strip("<>") == value.strip("<>")
        elif key == "after":
            found = seconds > int(value)
        elif key == "before":
            found = seconds < int(value)
        elif key == "is" and value == "unread":
            found = "UNREAD" in labels
        elif key == "category" and value != "primary":
            found = f"CATEGORY_{value.upper()}" in labels
        elif key == "in" and value in IN_LABELS:
            found = IN_LABELS[value] in labels
        elif key in ("from", "to"):
            found = value.lower() in headers.get(key.capitalize(), "").lower()
        elif key == "":
            found = any(value.lower() in headers.get(name, "").lower() for name in WORD_HEADERS)
        else:
            raise AssertionError(f"the fake mailbox does not understand {key}:{value}")
        if found == term.negate:
            return False
    return True
