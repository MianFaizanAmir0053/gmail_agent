from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.google.calendar import (
    CalendarClient,
    CalendarUnavailableError,
    NotUtcError,
    WriteRejectedError,
    event_body,
)

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


# --- writes that can be finished (M17, D3) -----------------------------------------


class Calendar:
    """A calendar that keeps its events, answering `409` for a taken id and
    `404` for an unknown one, as Google does. Calendars named in `hidden`
    answer `404` to everything, as one the token cannot see does; a body
    with a guest named `bad` is refused with a `400`."""

    def __init__(self, *, hidden: frozenset[str] = frozenset()) -> None:
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self.inserts = 0
        self.hidden = hidden

    def events_api(self) -> _EventsApi:
        return _EventsApi(self)


class _EventsApi:
    def __init__(self, calendar: Calendar) -> None:
        self._calendar = calendar
        self._call: Any = None

    def insert(self, *, calendarId: str, body: dict[str, Any]) -> _EventsApi:  # noqa: N803
        def run() -> dict[str, Any]:
            key = (calendarId, body["id"])
            if calendarId in self._calendar.hidden:
                raise _http_error(404)
            if {"email": "bad"} in body.get("attendees", []):
                raise _http_error(400)
            if key in self._calendar.events:
                raise _http_error(409)
            self._calendar.inserts += 1
            self._calendar.events[key] = dict(body, status="confirmed")
            return {"id": body["id"]}

        self._call = run
        return self

    def get(self, *, calendarId: str, eventId: str) -> _EventsApi:  # noqa: N803
        def run() -> dict[str, Any]:
            event = self._calendar.events.get((calendarId, eventId))
            if event is None or calendarId in self._calendar.hidden:
                raise _http_error(404)
            return {"id": eventId, "status": event["status"]}

        self._call = run
        return self

    def list(self, *, calendarId: str, **kwargs: Any) -> _EventsApi:  # noqa: N803
        def run() -> dict[str, Any]:
            if calendarId in self._calendar.hidden:
                raise _http_error(404)
            return {"items": []}

        self._call = run
        return self

    def execute(self) -> dict[str, Any]:
        result: dict[str, Any] = self._call()
        return result


class _Service:
    def __init__(self, calendar: Calendar) -> None:
        self._calendar = calendar

    def events(self) -> _EventsApi:
        return self._calendar.events_api()


def _http_error(status: int) -> Exception:
    from types import SimpleNamespace

    from googleapiclient.errors import HttpError

    error: Exception = HttpError(SimpleNamespace(status=status, reason="test"), b"")
    return error


BODY = event_body(
    title="Sync",
    start_utc=START,
    end_utc=END,
    timezone="Asia/Karachi",
    attendees=["sara@example.com"],
    location=None,
    description="Created by mailagent from message m1.",
)


def test_an_insert_carries_its_own_id_and_the_exact_body() -> None:
    calendar = Calendar()
    client = CalendarClient(_Service(calendar), "cal_test", dry_run=False)

    assert client.insert("cal_test", BODY, event_id="ma0123456789") == "ma0123456789"
    stored = calendar.events[("cal_test", "ma0123456789")]
    assert stored["summary"] == "Sync" and stored["attendees"] == [{"email": "sara@example.com"}]


def test_a_taken_id_is_the_event_already_made_not_a_failure() -> None:
    """A re-drive that inserts again gets a 409: the event exists, and is used."""
    calendar = Calendar()
    client = CalendarClient(_Service(calendar), "cal_test", dry_run=False)
    client.insert("cal_test", BODY, event_id="ma0123456789")

    assert client.insert("cal_test", BODY, event_id="ma0123456789") == "ma0123456789"
    assert calendar.inserts == 1


def test_the_lookup_finds_an_event_on_the_calendar_it_was_made_on() -> None:
    calendar = Calendar()
    client = CalendarClient(_Service(calendar), "cal_new", dry_run=False)
    client.insert("cal_old", BODY, event_id="ma0123456789")

    assert client.find("cal_old", "ma0123456789") == "ma0123456789"
    assert client.find("cal_new", "ma0123456789") is None


def test_a_deleted_event_is_still_found_so_it_is_never_made_again() -> None:
    calendar = Calendar()
    client = CalendarClient(_Service(calendar), "cal_test", dry_run=False)
    client.insert("cal_test", BODY, event_id="ma0123456789")
    calendar.events[("cal_test", "ma0123456789")]["status"] = "cancelled"

    assert client.find("cal_test", "ma0123456789") == "ma0123456789"


def test_under_dry_run_nothing_is_inserted() -> None:
    """The client's own guard, behind the registry's."""
    calendar = Calendar()
    client = CalendarClient(_Service(calendar), "cal_test", dry_run=True)

    assert client.insert("cal_test", BODY, event_id="ma0123456789") is None
    assert calendar.inserts == 0


def test_a_request_google_refuses_outright_is_not_tried_again() -> None:
    """A `400`, such as a malformed guest: another attempt would get the same."""
    client = CalendarClient(_Service(Calendar()), "cal_test", dry_run=False)
    body = dict(BODY, attendees=[{"email": "bad"}])

    with pytest.raises(WriteRejectedError):
        client.insert("cal_test", body, event_id="ma0123456789")


def test_an_event_missing_from_a_readable_calendar_is_not_there() -> None:
    client = CalendarClient(_Service(Calendar()), "cal_test", dry_run=False)

    assert client.find("cal_test", "ma0123456789") is None


def test_a_calendar_the_token_cannot_see_proves_nothing_about_its_events() -> None:
    """Google answers `404` for both. Giving up on a write must not take the
    calendar's silence for the event's absence (D3)."""
    calendar = Calendar(hidden=frozenset({"cal_gone"}))
    client = CalendarClient(_Service(calendar), "cal_test", dry_run=False)

    with pytest.raises(CalendarUnavailableError):
        client.find("cal_gone", "ma0123456789")
