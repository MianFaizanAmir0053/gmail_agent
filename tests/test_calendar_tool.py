from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.google.calendar import BusyInterval, CalendarClient
from app.tools.calendar_tool import (
    CALENDAR_TOOL,
    CreateEventInput,
    check_conflicts,
    overlaps,
)

START = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
END = START + timedelta(hours=1)


def _args(**overrides: object) -> CreateEventInput:
    base: dict[str, object] = {
        "title": "Design review",
        "start_utc": START,
        "end_utc": END,
        "timezone": "Asia/Karachi",
        "attendees": ["sara@example.com"],
        "location": None,
        "description": None,
    }
    return CreateEventInput.model_validate(base | overrides)


class FakeEvents:
    def __init__(self) -> None:
        self.inserted: list[dict[str, Any]] = []

    def insert(self, *, calendarId: str, body: dict[str, Any]) -> FakeEvents:  # noqa: N803
        self.inserted.append(body)
        return self

    def execute(self) -> dict[str, Any]:
        return {"id": "evt_123"}


class FakeFreebusy:
    def __init__(self, busy: list[dict[str, str]]) -> None:
        self._busy = busy

    def query(self, *, body: dict[str, Any]) -> FakeFreebusy:
        return self

    def execute(self) -> dict[str, Any]:
        return {"calendars": {"cal_test": {"busy": self._busy}}}


class FakeService:
    def __init__(self, busy: list[dict[str, str]] | None = None) -> None:
        self.events_resource = FakeEvents()
        self._busy = busy or []

    def events(self) -> FakeEvents:
        return self.events_resource

    def freebusy(self) -> FakeFreebusy:
        return FakeFreebusy(self._busy)


# --- tool definition -------------------------------------------------------


def test_tool_declares_every_field_the_executor_needs() -> None:
    """The declaration and CreateEventInput must not drift apart."""
    declared = set(CALENDAR_TOOL["parameters"]["properties"])
    assert declared == set(CreateEventInput.model_fields)


def test_only_genuinely_optional_arguments_are_omitted_from_required() -> None:
    schema = CALENDAR_TOOL["parameters"]
    assert set(schema["required"]) == set(schema["properties"]) - {"location", "description"}


def test_schema_avoids_constructs_gemini_rejects() -> None:
    """The OpenAPI subset has no additionalProperties and no anyOf-null."""
    schema = CALENDAR_TOOL["parameters"]
    assert "additionalProperties" not in schema
    assert all("anyOf" not in prop for prop in schema["properties"].values())


# --- overlap arithmetic ----------------------------------------------------


def test_back_to_back_events_do_not_conflict() -> None:
    """An event ending exactly when ours starts is not a clash, or every
    consecutive meeting would flag."""
    busy = [BusyInterval(start=START - timedelta(hours=1), end=START)]
    assert overlaps(START, END, busy) == []


def test_touching_at_the_end_does_not_conflict() -> None:
    busy = [BusyInterval(start=END, end=END + timedelta(hours=1))]
    assert overlaps(START, END, busy) == []


def test_partial_overlap_conflicts() -> None:
    busy = [BusyInterval(start=START + timedelta(minutes=30), end=END + timedelta(hours=1))]
    assert len(overlaps(START, END, busy)) == 1


def test_fully_contained_busy_block_conflicts() -> None:
    busy = [BusyInterval(start=START + timedelta(minutes=10), end=START + timedelta(minutes=20))]
    assert len(overlaps(START, END, busy)) == 1


def test_enclosing_busy_block_conflicts() -> None:
    busy = [BusyInterval(start=START - timedelta(hours=1), end=END + timedelta(hours=1))]
    assert len(overlaps(START, END, busy)) == 1


def test_conflict_check_reads_the_calendar() -> None:
    service = FakeService(
        busy=[{"start": "2026-08-19T11:30:00+00:00", "end": "2026-08-19T12:30:00+00:00"}]
    )
    calendar = CalendarClient(service, "cal_test", dry_run=True)

    check = check_conflicts(calendar, _args())

    assert check.has_conflict
    assert "Overlaps 1" in check.describe()


def test_conflict_check_runs_even_in_dry_run() -> None:
    """Availability is read-only, and the approval card needs it before any write."""
    calendar = CalendarClient(FakeService(), "cal_test", dry_run=True)
    assert check_conflicts(calendar, _args()).describe() == "No conflicts."
