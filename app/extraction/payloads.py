"""Wire models -- what the model is asked to emit, and how it becomes a contract.

## Why the model is never asked for UTC

The obvious schema asks for `start_utc` directly. Don't. Converting "4pm Karachi
on 19 August" to UTC is arithmetic over a timezone database, including DST rules
that change by year and jurisdiction, and models get it wrong in ways that look
plausible -- an hour off, silently, for half the year.

So the model reports what it actually read: a **local wall-clock time** plus the
**IANA zone that time was expressed in**. `zoneinfo` does the arithmetic. The
model does language; the standard library does calendars.

Kept separate from `app.contracts` on purpose: this schema is shaped by what is
easy for a model to emit reliably, and it should be free to change without
touching the type the rest of the system passes around.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from app.contracts import ExtractionResult


class InvalidPayloadError(ValueError):
    """The model produced structurally valid JSON that cannot become an event."""


def response_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic's JSON schema, cleaned of keys the API rejects.

    `extra="forbid"` is worth keeping on the payload models -- it is what makes
    `model_validate_json` reject a response with junk fields -- but it makes
    Pydantic emit `additionalProperties: false`, and the endpoint 400s on that
    key. So we validate strictly on our side and send a scrubbed schema.

    Only that one key is removed. Stripping by name is blunt: schema keywords
    and field names share a namespace, and a first attempt that also dropped
    `title` as "documentation noise" deleted `properties.title` -- a real field
    -- leaving it listed in `required` and nowhere else. Anything added here
    must be safe against colliding with a payload field name.
    """
    cleaned: dict[str, Any] = _strip_additional_properties(model.model_json_schema())
    return cleaned


def _strip_additional_properties(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _strip_additional_properties(value)
            for key, value in node.items()
            if key != "additionalProperties"
        }
    if isinstance(node, list):
        return [_strip_additional_properties(item) for item in node]
    return node


def required_fields(model: type[BaseModel]) -> set[str]:
    """Fields the model must emit, derived from the Pydantic definition.

    Gemini takes the Pydantic class directly as a response schema and derives
    `required` from it, so a field given a default silently becomes optional and
    the model is free to omit it. Absent and null are different answers -- "no
    location given" versus "didn't look" -- so every payload field is declared
    without a default, and a test asserts it stays that way.
    """
    return {name for name, f in model.model_fields.items() if f.is_required()}


class ClassifyPayload(BaseModel):
    """Cheap first pass: is this worth a full extraction?"""

    model_config = ConfigDict(extra="forbid")

    is_meeting: bool = Field(
        description=(
            "True only if this message should produce a calendar event. "
            "A cancellation, a newsletter advertising a webinar, or a vague "
            "'let's meet sometime' are all false."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="One sentence. Why this verdict.")


class ExtractionPayload(BaseModel):
    """Full extraction. Times are local wall-clock, never UTC."""

    model_config = ConfigDict(extra="forbid")

    is_meeting: bool
    title: str | None = Field(description="Short event title, or null if not a meeting.")
    start_local: str | None = Field(
        description=(
            "Local wall-clock start as ISO 8601 without any offset or Z, "
            "e.g. '2026-08-19T16:00:00'. Do NOT convert to UTC."
        )
    )
    end_local: str | None = Field(
        description="Local wall-clock end, same format. Never null when start_local is set."
    )
    timezone: str | None = Field(
        description=(
            "IANA zone name the times were expressed in, e.g. 'Asia/Karachi' or "
            "'America/Los_Angeles'. Never an offset like '+05:00'."
        )
    )
    attendees: list[str] = Field(description="Email addresses of other participants.")
    location: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


def _to_utc(local: str, zone: ZoneInfo, field: str) -> datetime:
    try:
        naive = datetime.fromisoformat(local)
    except ValueError as exc:
        raise InvalidPayloadError(f"{field} is not ISO 8601: {local!r}") from exc

    if naive.tzinfo is not None:
        # The model was told not to attach an offset. If it did anyway, trust the
        # offset it actually wrote rather than re-stamping it with `zone` -- that
        # would shift a correct instant.
        return naive.astimezone(UTC)
    return naive.replace(tzinfo=zone).astimezone(UTC)


def to_extraction_result(payload: ExtractionPayload, *, owner_email: str = "") -> ExtractionResult:
    """Convert wire payload to the domain contract, doing the timezone maths."""
    if not payload.is_meeting:
        return ExtractionResult(
            is_meeting=False,
            confidence=payload.confidence,
            reasoning=payload.reasoning,
        )

    if payload.start_local is None or payload.timezone is None:
        raise InvalidPayloadError("is_meeting is true but start_local or timezone is missing")

    try:
        zone = ZoneInfo(payload.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidPayloadError(f"Unknown IANA zone: {payload.timezone!r}") from exc

    if payload.end_local is None:
        raise InvalidPayloadError("end_local is required when start_local is set")

    start_utc = _to_utc(payload.start_local, zone, "start_local")
    end_utc = _to_utc(payload.end_local, zone, "end_local")

    if end_utc <= start_utc:
        raise InvalidPayloadError(f"end ({end_utc}) is not after start ({start_utc})")

    owner = owner_email.strip().lower()
    attendees = sorted({a.strip().lower() for a in payload.attendees if a.strip()} - {owner})

    return ExtractionResult(
        is_meeting=True,
        title=payload.title,
        start_utc=start_utc,
        end_utc=end_utc,
        timezone=payload.timezone,
        attendees=attendees,
        location=payload.location,
        confidence=payload.confidence,
        reasoning=payload.reasoning,
    )
