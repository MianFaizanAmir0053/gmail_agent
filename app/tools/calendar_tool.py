"""`create_calendar_event` as a model-callable tool.

## Tool use vs structured output, precisely

The plan framed this as "the model calls the tool, you don't call the function
after". That is nearly right but worth stating properly, because it comes up in
interviews:

- **Extraction** wants *structured output*. There is no action to take; we want a
  schema-validated object back. That is `app.extraction`.
- **The calendar write** wants a *tool*. The model decides whether to call it and
  with what arguments; the harness owns execution, validation, and the approval
  gate.

Both are used, for different jobs. The meaningful distinction is not that the
function is never invoked by our code -- something has to make the HTTP call.
It is that the *decision* and the *arguments* belong to the model, while
authorisation and side effects stay with the harness.

The declared schema is advisory rather than enforced, so `CreateEventInput`
re-validates whatever arrives before any of it reaches Google Calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.google.calendar import BusyInterval, CalendarClient


class CreateEventInput(BaseModel):
    """Arguments the model supplies. Times are UTC here, unlike the extraction
    payload -- by this point conversion has already happened and been checked."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Short event title.")
    start_utc: datetime = Field(description="ISO 8601 with offset or Z.")
    end_utc: datetime = Field(description="ISO 8601 with offset or Z. Must be after start_utc.")
    timezone: str = Field(description="IANA zone the times were expressed in.")
    attendees: list[str] = Field(description="Other participants' email addresses.")
    location: str | None = Field(description="Location, or null.")
    description: str | None = Field(description="Body text for the event, or null.")


CALENDAR_TOOL: dict[str, Any] = {
    "name": "create_calendar_event",
    "description": (
        "Create a calendar event. Call this once the meeting details are settled "
        "and a human has approved them. Do not call it to check availability."
    ),
    # Gemini function-declaration shape: an OpenAPI-subset `parameters` object.
    # Written out by hand rather than derived from the Pydantic model, because
    # Pydantic emits constructs the subset rejects -- `additionalProperties`, and
    # `anyOf: [{type: string}, {type: null}]` for optional fields, where Gemini
    # wants `nullable`. Seven fields is cheaper to maintain than schema surgery.
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short event title."},
            "start_utc": {
                "type": "string",
                "description": "ISO 8601 with offset or Z.",
            },
            "end_utc": {
                "type": "string",
                "description": "ISO 8601 with offset or Z. Must be after start_utc.",
            },
            "timezone": {
                "type": "string",
                "description": "IANA zone the times were expressed in, e.g. Asia/Karachi.",
            },
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Other participants' email addresses.",
            },
            "location": {"type": "string", "nullable": True},
            "description": {"type": "string", "nullable": True},
        },
        "required": ["title", "start_utc", "end_utc", "timezone", "attendees"],
    },
}


FREEBUSY_TOOL: dict[str, Any] = {
    "name": "freebusy_check",
    "description": (
        "Check whether a time range is already busy on the calendar. Call this before "
        "approving a proposed meeting time, to confirm the slot is actually free. "
        "Read-only: it books nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "start_utc": {"type": "string", "description": "ISO 8601 with offset or Z."},
            "end_utc": {"type": "string", "description": "ISO 8601 with offset or Z."},
        },
        "required": ["start_utc", "end_utc"],
    },
}


class FreeBusyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_utc: datetime
    end_utc: datetime


def execute_freebusy(calendar: CalendarClient, raw_args: dict[str, Any]) -> dict[str, Any]:
    """Run a `freebusy_check` call. Never raises.

    A reviewer that asked a malformed question should get an answer it can read
    and retry, not an exception that ends the extraction it was checking.
    """
    try:
        args = FreeBusyInput.model_validate(raw_args)
    except ValidationError as exc:
        return {"error": f"Invalid arguments: {exc.errors()[0]['msg']}", "busy": []}

    try:
        busy = calendar.freebusy(args.start_utc, args.end_utc)
    except Exception as exc:
        # The model gets the message, not a traceback. A calendar outage should
        # cost the reviewer one check, not the whole extraction.
        return {"error": f"{type(exc).__name__}: {exc}", "busy": []}

    if not busy:
        return {"busy": [], "note": "The slot is free."}

    return {
        "busy": [
            {"start": interval.start.isoformat(), "end": interval.end.isoformat()}
            for interval in busy
        ]
    }


def overlaps(start: datetime, end: datetime, busy: list[BusyInterval]) -> list[BusyInterval]:
    """Busy intervals colliding with the proposed slot.

    Half-open comparison: an event ending exactly when another starts is not a
    conflict, or every back-to-back meeting would flag.
    """
    return [b for b in busy if b.start < end and start < b.end]


@dataclass(frozen=True, slots=True)
class ConflictCheck:
    conflicts: list[BusyInterval]

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)

    def describe(self) -> str:
        if not self.conflicts:
            return "No conflicts."
        parts = [f"{c.start:%Y-%m-%d %H:%M}-{c.end:%H:%M} UTC" for c in self.conflicts]
        return f"Overlaps {len(parts)} existing event(s): {', '.join(parts)}"


def check_conflicts(calendar: CalendarClient, args: CreateEventInput) -> ConflictCheck:
    """Read-only availability check, run before a human is asked to approve.

    Deliberately separate from the write, which only the registry runs
    (`app/policy/registry.py`): the approval card needs to show the conflict
    *before* anything is written.
    """
    busy = calendar.freebusy(args.start_utc, args.end_utc)
    return ConflictCheck(conflicts=overlaps(args.start_utc, args.end_utc, busy))
