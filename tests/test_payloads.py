from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.extraction.payloads import (
    ClassifyPayload,
    ExtractionPayload,
    InvalidPayloadError,
    required_fields,
    to_extraction_result,
)


def _payload(**overrides: object) -> ExtractionPayload:
    base: dict[str, object] = {
        "is_meeting": True,
        "title": "Design review",
        "start_local": "2026-08-19T16:00:00",
        "end_local": "2026-08-19T17:00:00",
        "timezone": "Asia/Karachi",
        "attendees": ["sara@example.com"],
        "location": None,
        "confidence": 0.9,
        "reasoning": "",
    }
    return ExtractionPayload.model_validate(base | overrides)


# --- schema ----------------------------------------------------------------


def test_every_extraction_field_is_required() -> None:
    """Absent and null mean different things; the model must be forced to choose.

    Gemini derives `required` from the Pydantic model, so giving any field a
    default would silently let the model omit it -- "no location given" would
    become indistinguishable from "didn't look".
    """
    assert required_fields(ExtractionPayload) == set(ExtractionPayload.model_fields)


def test_every_classify_field_is_required() -> None:
    assert required_fields(ClassifyPayload) == set(ClassifyPayload.model_fields)


def test_payloads_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match=r"[Ee]xtra"):
        ClassifyPayload.model_validate(
            {"is_meeting": True, "confidence": 0.5, "reasoning": "", "surprise": 1}
        )


# --- timezone arithmetic ---------------------------------------------------


def test_local_time_converts_to_utc() -> None:
    result = to_extraction_result(_payload())
    assert result.start_utc == datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
    assert result.end_utc == datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def test_originating_zone_is_preserved_not_normalised() -> None:
    """The instant goes to UTC; the zone it was said in is kept as written."""
    result = to_extraction_result(_payload())
    assert result.timezone == "Asia/Karachi"


def test_daylight_saving_is_handled_by_zoneinfo_not_the_model() -> None:
    """9am Los Angeles is 16:00Z in August (PDT) but 17:00Z in December (PST)."""
    summer = to_extraction_result(
        _payload(
            start_local="2026-08-20T09:00:00",
            end_local="2026-08-20T10:00:00",
            timezone="America/Los_Angeles",
        )
    )
    winter = to_extraction_result(
        _payload(
            start_local="2026-12-20T09:00:00",
            end_local="2026-12-20T10:00:00",
            timezone="America/Los_Angeles",
        )
    )
    assert summer.start_utc == datetime(2026, 8, 20, 16, 0, tzinfo=UTC)
    assert winter.start_utc == datetime(2026, 12, 20, 17, 0, tzinfo=UTC)


def test_all_day_spans_local_midnight_to_midnight() -> None:
    result = to_extraction_result(
        _payload(start_local="2026-08-21T00:00:00", end_local="2026-08-22T00:00:00")
    )
    assert result.start_utc == datetime(2026, 8, 20, 19, 0, tzinfo=UTC)
    assert result.end_utc == datetime(2026, 8, 21, 19, 0, tzinfo=UTC)


def test_offset_supplied_against_instructions_is_trusted_not_restamped() -> None:
    """Re-stamping an explicit offset with the IANA zone would shift a correct instant."""
    result = to_extraction_result(
        _payload(start_local="2026-08-19T11:00:00+00:00", end_local="2026-08-19T12:00:00+00:00")
    )
    assert result.start_utc == datetime(2026, 8, 19, 11, 0, tzinfo=UTC)


# --- rejection -------------------------------------------------------------


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(InvalidPayloadError, match="Unknown IANA zone"):
        to_extraction_result(_payload(timezone="Mars/Olympus"))


def test_offset_string_as_timezone_is_rejected() -> None:
    with pytest.raises(InvalidPayloadError):
        to_extraction_result(_payload(timezone="+05:00"))


def test_unparseable_time_is_rejected() -> None:
    with pytest.raises(InvalidPayloadError, match="not ISO 8601"):
        to_extraction_result(_payload(start_local="next Tuesday"))


def test_missing_end_is_rejected() -> None:
    with pytest.raises(InvalidPayloadError, match="end_local is required"):
        to_extraction_result(_payload(end_local=None))


def test_end_before_start_is_rejected() -> None:
    with pytest.raises(InvalidPayloadError, match="not after"):
        to_extraction_result(_payload(end_local="2026-08-19T15:00:00"))


def test_meeting_without_timezone_is_rejected() -> None:
    with pytest.raises(InvalidPayloadError, match="missing"):
        to_extraction_result(_payload(timezone=None))


# --- attendees -------------------------------------------------------------


def test_owner_is_stripped_from_attendees() -> None:
    payload = _payload(attendees=["sara@example.com", "Me@Example.com"])
    result = to_extraction_result(payload, owner_email="me@example.com")
    assert result.attendees == ["sara@example.com"]


def test_attendees_are_deduplicated_and_sorted() -> None:
    payload = _payload(attendees=["b@example.com", "A@example.com", "b@example.com", "  "])
    assert to_extraction_result(payload).attendees == ["a@example.com", "b@example.com"]


# --- non-meetings ----------------------------------------------------------


def test_non_meeting_drops_event_fields_even_if_the_model_filled_them() -> None:
    payload = _payload(is_meeting=False, title="Leftover", attendees=["x@example.com"])
    result = to_extraction_result(payload)
    assert result.is_meeting is False
    assert result.title is None
    assert result.start_utc is None
    assert result.attendees == []
