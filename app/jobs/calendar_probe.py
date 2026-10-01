"""Does Google behave as M17's finishable writes assume? (D3)

    python -m app.jobs.calendar_probe

Run by the owner at the end tests, before `DRY_RUN` goes off. On the test
calendar only, with a fresh random id each run, it:

1. inserts an event, inserts it again, and expects a `409`;
2. deletes it, expects `events.get` to return it as `cancelled`, and expects
   another insert with the same id to answer `409`.

Those are the two facts a re-drive rests on: a taken id is refused rather
than booked twice, and a deleted event keeps its id, so a re-drive never
recreates an event the owner removed.

It is the one tool that ignores `DRY_RUN`, and says so before it writes.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from googleapiclient.errors import HttpError

from app.config import get_settings
from app.google.auth import build_service, load_credentials
from app.google.calendar import event_body


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str


def probe(service: Any, calendar_id: str, *, now: datetime) -> list[Check]:
    """Run the checks on `calendar_id`. The event is deleted again, whatever
    the checks find."""
    # Hex digits and "probe" are all base32hex, which Google requires.
    event_id = "probe" + secrets.token_hex(12)
    start = now + timedelta(days=1)
    body = {
        **event_body(
            title="mailagent calendar probe",
            start_utc=start,
            end_utc=start + timedelta(minutes=30),
            timezone="UTC",
            description="Created by app.jobs.calendar_probe. Safe to delete.",
        ),
        "id": event_id,
    }

    def insert() -> Any:
        return service.events().insert(calendarId=calendar_id, body=body).execute()

    try:
        insert()
        checks = [_refused(insert, "a second insert with the same id")]
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        checks.append(_kept(service, calendar_id, event_id))
        checks.append(_refused(insert, "an insert after the delete"))
        return checks
    finally:
        _clean_up(service, calendar_id, event_id)


def _refused(insert: Callable[[], Any], name: str) -> Check:
    """Expects Google to answer `409`: the id is taken."""
    try:
        insert()
    except HttpError as error:
        status = int(getattr(error.resp, "status", 0))
        return Check(name, status == 409, f"answered {status}")
    return Check(name, False, "was accepted: the id was not seen as taken")


def _kept(service: Any, calendar_id: str, event_id: str) -> Check:
    """Expects a deleted event to keep its id, as `cancelled`."""
    name = "the deleted event, asked for by id"
    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as error:
        return Check(name, False, f"answered {int(getattr(error.resp, 'status', 0))}")
    status = event.get("status")
    return Check(name, status == "cancelled", f"came back {status!r}")


def _clean_up(service: Any, calendar_id: str, event_id: str) -> None:
    """Delete the event if a check left it live: an insert Google accepted
    when it should have refused it, or a check that raised."""
    try:
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError:
        return  # nothing there to delete
    if event.get("status") != "cancelled":
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()


def main() -> None:
    settings = get_settings()
    calendar_id = settings.test_calendar_id
    if calendar_id is None or calendar_id == "primary":
        raise SystemExit("The probe runs on the test calendar only: set TEST_CALENDAR_ID.")

    print(
        "The calendar probe ignores DRY_RUN. It writes one event to the test calendar,"
        " then deletes it."
    )
    service = build_service("calendar", "v3", load_credentials(settings))
    checks = probe(service, calendar_id, now=datetime.now(UTC))
    for check in checks:
        print(f"  {'ok  ' if check.passed else 'FAIL'}  {check.name}: {check.detail}")

    if not all(check.passed for check in checks):
        raise SystemExit("Google does not behave as M17 assumes. Leave DRY_RUN on.")
    print("Google behaves as M17 assumes.")


if __name__ == "__main__":
    main()
