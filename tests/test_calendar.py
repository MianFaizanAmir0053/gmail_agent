from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.google.calendar import (
    CalendarClient,
    CalendarEvent,
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


# --- two calendars: reads from the primary (M19, D3) ------------------------


class _Reads:
    """Answers free/busy and one page of events with what it was given, and
    writes down every request."""

    def __init__(self, *, free_busy: dict[str, Any] | None = None, page: Any = None) -> None:
        self.free_busy = free_busy or {}
        self.page = page or {"items": []}
        self.asked: list[tuple[str, dict[str, Any]]] = []
        self.inserted: list[str] = []
        self._answer: Any = None

    def freebusy(self) -> _Reads:
        return self

    def events(self) -> _Reads:
        return self

    def query(self, *, body: dict[str, Any]) -> _Reads:
        self.asked.append(("freebusy", body))
        self._answer = self.free_busy
        return self

    def list(self, **kwargs: Any) -> _Reads:
        self.asked.append(("list", kwargs))
        self._answer = self.page
        return self

    def insert(self, *, calendarId: str, body: dict[str, Any]) -> _Reads:  # noqa: N803
        self.inserted.append(calendarId)
        self._answer = {"id": "evt_123"}
        return self

    def execute(self) -> Any:
        return self._answer


def _reader(service: _Reads, *, dry_run: bool = False) -> CalendarClient:
    return CalendarClient(service, "cal_test", dry_run=dry_run)


def test_the_read_calendar_is_the_primary_and_writes_keep_their_own() -> None:
    client = _reader(_Reads())
    assert (client.read_calendar_id, client.calendar_id) == ("primary", "cal_test")


def test_free_busy_reads_the_primary() -> None:
    """A card's "no conflicts" means the owner's real calendar is free (D3)."""
    busy = {"start": "2026-08-18T10:15:00Z", "end": "2026-08-18T11:00:00Z"}
    service = _Reads(free_busy={"calendars": {"primary": {"busy": [busy]}}})

    intervals = _reader(service).freebusy(START, END)

    assert service.asked == [
        (
            "freebusy",
            {
                "timeMin": "2026-08-18T10:00:00+00:00",
                "timeMax": "2026-08-18T10:30:00+00:00",
                "items": [{"id": "primary"}],
            },
        )
    ]
    assert [(b.start, b.end) for b in intervals] == [
        (START + timedelta(minutes=15), START + timedelta(hours=1))
    ]


def test_writes_go_to_the_write_calendar() -> None:
    service = _Reads()
    _reader(service).create_event(title="Sync", start_utc=START, end_utc=END, timezone="UTC")
    assert service.inserted == ["cal_test"]


@pytest.mark.parametrize(
    "answer",
    [
        {"calendars": {"primary": {"busy": [], "errors": [{"reason": "notFound"}]}}},
        {"calendars": {}},
        {},
    ],
)
def test_free_busy_that_cannot_be_read_is_never_free(answer: dict[str, Any]) -> None:
    """Google reports an unreadable calendar beside an empty busy list."""
    with pytest.raises(CalendarUnavailableError):
        _reader(_Reads(free_busy=answer)).freebusy(START, END)


def _item(**fields: Any) -> dict[str, Any]:
    return {
        "summary": "Design review",
        "status": "confirmed",
        "start": {"dateTime": "2026-08-18T15:00:00+05:00"},
        "end": {"dateTime": "2026-08-18T16:00:00+05:00"},
        "organizer": {"email": "sara@example.com"},
    } | fields


def test_events_are_read_from_the_primary_one_page_at_a_time() -> None:
    service = _Reads(page={"items": [], "nextPageToken": "next"})

    events = _reader(service, dry_run=True).list_events(START, END)

    kind, request = service.asked[0]
    assert kind == "list"
    assert request["calendarId"] == "primary"
    assert (request["timeMin"], request["timeMax"]) == (
        "2026-08-18T10:00:00+00:00",
        "2026-08-18T10:30:00+00:00",
    )
    assert request["singleEvents"] is True and request["orderBy"] == "startTime"
    assert request["maxResults"] == 50
    assert events.more is True and events.events == ()


def test_an_event_comes_back_with_its_times_organiser_and_title() -> None:
    owners = _item(summary="Focus", organizer={"email": "me@example.com", "self": True})
    service = _Reads(page={"items": [_item(), owners]})

    events = _reader(service).list_events(START, END)

    assert events.more is False
    first, second = events.events
    assert first == CalendarEvent(
        title="Design review",
        start=datetime(2026, 8, 18, 10, tzinfo=UTC),
        end=datetime(2026, 8, 18, 11, tzinfo=UTC),
        all_day=False,
        organizer="sara@example.com",
        by_owner=False,
    )
    assert (second.title, second.by_owner) == ("Focus", True)


def test_an_all_day_event_spans_its_days_in_the_calendars_zone() -> None:
    item = _item(start={"date": "2026-08-18"}, end={"date": "2026-08-19"})
    service = _Reads(page={"timeZone": "Asia/Karachi", "items": [item]})

    (event,) = _reader(service).list_events(START, END).events

    assert event.all_day is True
    assert (event.start, event.end) == (
        datetime(2026, 8, 17, 19, tzinfo=UTC),
        datetime(2026, 8, 18, 19, tzinfo=UTC),
    )


def test_an_event_with_no_title_has_an_empty_one() -> None:
    item = _item()
    del item["summary"]
    (event,) = _reader(_Reads(page={"items": [item]})).list_events(START, END).events
    assert event.title == ""


def test_cancelled_declined_and_timeless_events_are_left_out() -> None:
    declined = _item(attendees=[{"self": True, "responseStatus": "declined"}])
    someone_else_declined = _item(
        attendees=[{"email": "x@example.com", "responseStatus": "declined"}]
    )
    page = {
        "items": [
            _item(status="cancelled"),
            declined,
            _item(start={}),
            someone_else_declined,
        ]
    }

    events = _reader(_Reads(page=page)).list_events(START, END).events

    assert len(events) == 1


def test_a_naive_range_is_refused_before_reading() -> None:
    service = _Reads()
    with pytest.raises(NotUtcError):
        _reader(service).list_events(datetime(2026, 8, 18, 10), END)
    assert service.asked == []
