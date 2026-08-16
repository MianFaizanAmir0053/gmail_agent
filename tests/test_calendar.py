from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.google.calendar import CalendarClient, NotUtcError

START = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)


class FakeEvents:
    """Records calls instead of making them."""

    def __init__(self) -> None:
        self.inserted: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def insert(self, *, calendarId: str, body: dict[str, Any]) -> FakeEvents:  # noqa: N803
        self.inserted.append(body)
        return self

    def delete(self, *, calendarId: str, eventId: str) -> FakeEvents:  # noqa: N803
        self.deleted.append(eventId)
        return self

    def execute(self) -> dict[str, Any]:
        return {"id": "evt_123"}


class FakeService:
    def __init__(self) -> None:
        self.events_resource = FakeEvents()

    def events(self) -> FakeEvents:
        return self.events_resource


def _client(*, dry_run: bool) -> tuple[CalendarClient, FakeService]:
    service = FakeService()
    return CalendarClient(service, "cal_test", dry_run=dry_run), service


def test_dry_run_creates_nothing() -> None:
    client, service = _client(dry_run=True)

    event_id = client.create_event(
        title="Sync", start_utc=START, end_utc=END, timezone="Asia/Karachi"
    )

    assert event_id is None
    assert service.events_resource.inserted == []


def test_dry_run_deletes_nothing() -> None:
    client, service = _client(dry_run=True)
    assert client.delete_event("evt_123") is False
    assert service.events_resource.deleted == []


def test_create_event_returns_id_and_sends_body() -> None:
    client, service = _client(dry_run=False)

    event_id = client.create_event(
        title="Sync",
        start_utc=START,
        end_utc=END,
        timezone="Asia/Karachi",
        attendees=["sara@example.com"],
        location="Room 2",
    )

    assert event_id == "evt_123"
    body = service.events_resource.inserted[0]
    assert body["summary"] == "Sync"
    assert body["start"] == {"dateTime": "2026-08-18T10:00:00+00:00", "timeZone": "Asia/Karachi"}
    assert body["attendees"] == [{"email": "sara@example.com"}]
    assert body["location"] == "Room 2"


def test_optional_fields_are_omitted_not_null() -> None:
    client, service = _client(dry_run=False)
    client.create_event(title="Sync", start_utc=START, end_utc=END, timezone="UTC")

    body = service.events_resource.inserted[0]
    assert "attendees" not in body
    assert "location" not in body
    assert "description" not in body


def test_naive_datetime_is_rejected() -> None:
    client, _ = _client(dry_run=False)

    with pytest.raises(NotUtcError):
        client.create_event(
            title="Sync",
            start_utc=datetime(2026, 8, 18, 10, 0),  # naive on purpose -- the bug under test
            end_utc=END,
            timezone="UTC",
        )


def test_naive_datetime_is_rejected_before_dry_run_short_circuit() -> None:
    """Validation must not be skipped just because nothing will be written."""
    client, _ = _client(dry_run=True)

    with pytest.raises(NotUtcError):
        client.create_event(
            title="Sync",
            start_utc=datetime(2026, 8, 18, 10, 0),  # naive on purpose -- the bug under test
            end_utc=END,
            timezone="UTC",
        )


def test_delete_event_calls_through() -> None:
    client, service = _client(dry_run=False)
    assert client.delete_event("evt_123") is True
    assert service.events_resource.deleted == ["evt_123"]
