"""What an approval is bound to (M17, D2).

The owner approves a card. What runs is whatever the checkpoint's extraction
holds when `act` gets there. The hash ties the two together: it is taken over
exactly the arguments the provider will receive, when the proposal parks, and
taken again at execution. If anything the owner could not have seen has
changed in between -- a title, a time, a guest, the target calendar, or the
code that turns an extraction into arguments -- the two differ and nothing
runs.

**Keyed.** The hash is kept for good, in the approvals and the audit log. A
plain SHA-256 of a title and a time can be confirmed by guessing them. An
HMAC under a key derived from `FERNET_KEY` cannot.

**Canonical.** The same event written two ways must hash the same, so the
form is fixed: sorted keys, times in UTC to the second, guests lower-cased,
de-duplicated and sorted. `HASH_VERSION` changes whenever the form does, which
sends every queued approval back to the owner rather than letting an old hash
match a new form by accident.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ValidationError

from app.contracts import ExtractionResult
from app.policy.scrub import shown
from app.tools.calendar_tool import CreateEventInput

HASH_VERSION = 1

HOLD = "calendar.create_hold"
"""T1: an event with no guests, on the owner's own calendar."""

INVITE = "calendar.create_invite"
"""T2: an event with guests, which leaves the owner's account."""

_KEY_INFO = b"mailagent-args-v1"


@dataclass(frozen=True, slots=True)
class Binding:
    """What a proposal row's hash is taken under: the calendar the action
    would write to, and the key. Built from the running session's settings,
    so a row is always bound as the current code would run it."""

    calendar_id: str
    key: bytes = field(repr=False)


def bound(
    message_id: str, proposed: Mapping[str, Any], binding: Binding
) -> tuple[str | None, str | None]:
    """The tool and the hash for a parked payload's `proposed` extraction.

    `(None, None)` when it cannot run -- no times, or not an extraction at
    all. Such a proposal has nothing to approve, and `decide()` refuses a
    Confirm on it as not ready.
    """
    try:
        extraction = ExtractionResult.model_validate(proposed)
    except ValidationError:
        return None, None
    if extraction.start_utc is None or extraction.end_utc is None:
        return None, None
    args = event_args(extraction, message_id)
    tool = tool_for(args.attendees)
    return tool, args_hash(binding.key, tool=tool, calendar_id=binding.calendar_id, args=args)


def tool_for(attendees: Sequence[str]) -> str:
    """Any guest makes an event an invitation: it reaches someone else."""
    return INVITE if attendees else HOLD


def event_args(extraction: ExtractionResult, message_id: str) -> CreateEventInput:
    """The arguments `act` runs for an extraction.

    The only place they are built: the hash at park and the call at execution
    both come from here, so the two can never drift apart. The title and the
    location are scrubbed here (M18, D6), so a checkpoint made before that is
    written as its card shows it.
    """
    assert extraction.start_utc is not None
    assert extraction.end_utc is not None
    return CreateEventInput(
        title=shown(extraction.title) or "(untitled)",
        start_utc=extraction.start_utc,
        end_utc=extraction.end_utc,
        timezone=extraction.timezone or "UTC",
        attendees=extraction.attendees,
        location=shown(extraction.location),
        description=f"Created by mailagent from message {message_id}.",
    )


def args_key(fernet_key: str) -> bytes:
    """The hash's key, derived from `FERNET_KEY` so no new secret is needed.

    A separate derivation (HKDF, with its own `info`) rather than the Fernet
    key itself, so the two uses can never be confused for one another.
    """
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO).derive(
        fernet_key.encode()
    )


def event_id_for(key: bytes, message_id: str, args_hash: str) -> str:
    """The calendar event's own id, the same on every attempt (M17, D3).

    Google accepts client-chosen ids of 5 to 1024 characters from base32hex
    (`0`-`9`, `a`-`v`). A later attempt asks for this id before inserting, and
    an insert that finds it taken gets a `409` rather than a second event.
    Keyed, like the hash, so the id says nothing about the event.
    """
    digest = hmac.new(key, f"{message_id}:{args_hash}".encode(), hashlib.sha256).digest()
    return "ma" + base64.b32hexencode(digest).decode().rstrip("=").lower()[:30]


def subject_hash(key: bytes, text: str) -> str:
    """A keyed hash of something the audit log must link to without naming,
    such as a contact's address (D7)."""
    return hmac.new(key, f"subject:{text}".encode(), hashlib.sha256).hexdigest()


def args_hash(key: bytes, *, tool: str, calendar_id: str, args: CreateEventInput) -> str:
    """HMAC-SHA256 over the canonical form, as 64 hex characters."""
    return hmac.new(key, _canonical(tool, calendar_id, args), hashlib.sha256).hexdigest()


def _canonical(tool: str, calendar_id: str, args: CreateEventInput) -> bytes:
    form = {
        "hash_version": HASH_VERSION,
        "tool": tool,
        "calendar_id": calendar_id,
        "title": args.title,
        "start_utc": _utc(args.start_utc),
        "end_utc": _utc(args.end_utc),
        "timezone": args.timezone,
        "attendees": sorted({a.strip().lower() for a in args.attendees}),
        "location": args.location,
        "description": args.description,
    }
    return json.dumps(form, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
