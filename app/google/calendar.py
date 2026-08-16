"""Calendar writes.

Every mutating method checks `dry_run` and returns without calling Google. The
flag is threaded through the constructor rather than read from settings inside
each method, so tests can exercise both paths without touching the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast


class NotUtcError(ValueError):
    """Naive or non-UTC datetime reached the Calendar layer."""


def _rfc3339(value: datetime, field: str) -> str:
    if value.tzinfo is None:
        raise NotUtcError(f"{field} is naive; extraction must produce tz-aware UTC datetimes")
    return value.astimezone(tz=value.tzinfo).isoformat()


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
        """Create an event. Returns the event ID, or None when dry-running.

        `timezone` is the IANA zone the humans involved actually think in. The
        timestamps stay UTC; the zone rides alongside so Google renders and
        recurs correctly across DST.
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

        if self._dry_run:
            return None

        created = cast(
            dict[str, Any],
            self._service.events().insert(calendarId=self._calendar_id, body=body).execute(),
        )
        return cast(str, created["id"])

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
