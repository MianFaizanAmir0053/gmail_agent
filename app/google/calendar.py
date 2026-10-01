"""Calendar writes.

Every mutating method checks `dry_run` and returns without calling Google. The
flag is threaded through the constructor rather than read from settings inside
each method, so tests can exercise both paths without touching the environment.

**Writes that can be finished (M17, D3).** `insert` sends an event with an id
chosen by us, and `find` asks for it by that id. A write interrupted halfway
is finished by asking first and inserting only if Google has no such event;
an insert that finds the id taken gets a `409`, which means the event exists.
Both take the calendar explicitly, because a later attempt must use the
calendar the owner approved, not whichever one the settings name today.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from googleapiclient.errors import HttpError


class NotUtcError(ValueError):
    """Naive or non-UTC datetime reached the Calendar layer."""


class WriteRejectedError(RuntimeError):
    """Google refused the request itself (`400`), such as a malformed guest
    address. Sending it again cannot succeed."""


class CalendarUnavailableError(RuntimeError):
    """The calendar itself cannot be read: unshared, deleted, or the token is
    another account's. An event's absence there proves nothing."""


def _rfc3339(value: datetime, field: str) -> str:
    if value.tzinfo is None:
        raise NotUtcError(f"{field} is naive; extraction must produce tz-aware UTC datetimes")
    return value.astimezone(tz=value.tzinfo).isoformat()


def event_body(
    *,
    title: str,
    start_utc: datetime,
    end_utc: datetime,
    timezone: str,
    attendees: list[str] | None = None,
    location: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """The exact request body Google receives for an event.

    `timezone` is the IANA zone the humans involved actually think in. The
    timestamps stay UTC; the zone rides alongside so Google renders and recurs
    correctly across DST. Optional fields are left out rather than sent as
    null.
    """
    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": _rfc3339(start_utc, "start_utc"), "timeZone": timezone},
        "end": {"dateTime": _rfc3339(end_utc, "end_utc"), "timeZone": timezone},
    }
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    return body


def _status(error: HttpError) -> int:
    return int(getattr(error.resp, "status", 0))


@dataclass(frozen=True, slots=True)
class BusyInterval:
    start: datetime
    end: datetime


class CalendarClient:
    def __init__(self, service: Any, calendar_id: str, *, dry_run: bool) -> None:
        self._service = service
        self._calendar_id = calendar_id
        self._dry_run = dry_run

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def calendar_id(self) -> str:
        """Where events go. Part of what an approval binds (M17, D2): approved
        for one calendar, an event must not be booked on another."""
        return self._calendar_id

    def create_event(
        self,
        *,
        title: str,
        start_utc: datetime,
        end_utc: datetime,
        timezone: str,
        attendees: list[str] | None = None,
        location: str | None = None,
        description: str | None = None,
    ) -> str | None:
        """Create an event with an id Google chooses. Returns the event ID, or
        None when dry-running.

        Only operator tools use this now (`app/google/smoke.py`); the agent's
        own writes go through the registry and `insert` (M17).
        """
        body = event_body(
            title=title,
            start_utc=start_utc,
            end_utc=end_utc,
            timezone=timezone,
            attendees=attendees,
            location=location,
            description=description,
        )

        if self._dry_run:
            return None

        created = cast(
            dict[str, Any],
            self._service.events().insert(calendarId=self._calendar_id, body=body).execute(),
        )
        return cast(str, created["id"])

    def insert(self, calendar_id: str, body: dict[str, Any], *, event_id: str) -> str | None:
        """Insert `body` with our own `event_id`. Returns the id, or None under
        `DRY_RUN`, when nothing is sent.

        A `409` means the id is taken: the event exists, made by an earlier
        attempt, so its id is returned as if this insert had made it. A `400`
        raises `WriteRejectedError`; anything else is raised as it came, to
        be tried again.
        """
        if self._dry_run:
            return None
        try:
            self._service.events().insert(
                calendarId=calendar_id, body={**body, "id": event_id}
            ).execute()
        except HttpError as error:
            status = _status(error)
            if status == 400:
                raise WriteRejectedError(f"the calendar refused the request ({status})") from error
            if status != 409:
                raise
        return event_id

    def find(self, calendar_id: str, event_id: str) -> str | None:
        """The event's id if `calendar_id` holds it, cancelled ones included,
        or None. A read, so `DRY_RUN` does not stop it.

        A cancelled event is one the owner deleted: finding it is what stops a
        later attempt from making it again.

        Google answers `404` for a calendar it will not show, too. So before a
        `404` is taken to mean "no such event", the calendar is read: if that
        fails, `CalendarUnavailableError` is raised rather than a guess.
        """
        try:
            found = cast(
                dict[str, Any],
                self._service.events().get(calendarId=calendar_id, eventId=event_id).execute(),
            )
        except HttpError as error:
            if _status(error) not in (404, 410):
                raise
            self._require_readable(calendar_id)
            return None
        return cast(str, found["id"])

    def _require_readable(self, calendar_id: str) -> None:
        """One page of one event: the cheapest read the events scope allows."""
        try:
            self._service.events().list(
                calendarId=calendar_id, maxResults=1, fields="items(id)"
            ).execute()
        except HttpError as error:
            raise CalendarUnavailableError(
                f"the calendar cannot be read ({_status(error)})"
            ) from error

    def delete_event(self, event_id: str) -> bool:
        """Delete an event. Returns False when dry-running."""
        if self._dry_run:
            return False
        self._service.events().delete(calendarId=self._calendar_id, eventId=event_id).execute()
        return True

    def freebusy(self, start_utc: datetime, end_utc: datetime) -> list[BusyInterval]:
        """Busy intervals on this calendar. Read-only, so it ignores dry_run."""
        response = cast(
            dict[str, Any],
            self._service.freebusy()
            .query(
                body={
                    "timeMin": _rfc3339(start_utc, "start_utc"),
                    "timeMax": _rfc3339(end_utc, "end_utc"),
                    "items": [{"id": self._calendar_id}],
                }
            )
            .execute(),
        )
        busy = response.get("calendars", {}).get(self._calendar_id, {}).get("busy", [])
        return [
            BusyInterval(
                start=datetime.fromisoformat(b["start"]),
                end=datetime.fromisoformat(b["end"]),
            )
            for b in busy
        ]
