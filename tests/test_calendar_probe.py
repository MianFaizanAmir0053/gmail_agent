"""The calendar probe (M17, D3): it checks what a finishable write rests on,
and cleans up after itself, whatever it finds."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from googleapiclient.errors import HttpError

from app.jobs.calendar_probe import probe

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


class Google:
    """One calendar, as Google keeps it: a taken id answers `409`, and a
    deleted event stays, `cancelled`. Each flag breaks one of those."""

    def __init__(
        self,
        *,
        sees_taken_ids: bool = True,
        keeps_deleted: bool = True,
        loses_first_reply: bool = False,
    ) -> None:
        self.sees_taken_ids = sees_taken_ids
        self.keeps_deleted = keeps_deleted
        self.loses_first_reply = loses_first_reply
        self.events_by_id: dict[str, dict[str, Any]] = {}
        self.calendars: set[str] = set()

    def events(self) -> _Call:
        return _Call(self)


class _Call:
    def __init__(self, google: Google) -> None:
        self._google = google
        self._run: Any = None

    def insert(self, *, calendarId: str, body: dict[str, Any]) -> _Call:  # noqa: N803
        def run() -> dict[str, Any]:
            self._google.calendars.add(calendarId)
            if body["id"] in self._google.events_by_id and self._google.sees_taken_ids:
                raise _error(409)
            self._google.events_by_id[body["id"]] = dict(body, status="confirmed")
            if self._google.loses_first_reply:
                self._google.loses_first_reply = False
                raise _error(503)  # made, but the reply never arrived
            return {"id": body["id"]}

        self._run = run
        return self

    def delete(self, *, calendarId: str, eventId: str) -> _Call:  # noqa: N803
        def run() -> dict[str, Any]:
            if self._google.keeps_deleted:
                self._google.events_by_id[eventId]["status"] = "cancelled"
            else:
                del self._google.events_by_id[eventId]
            return {}

        self._run = run
        return self

    def get(self, *, calendarId: str, eventId: str) -> _Call:  # noqa: N803
        def run() -> dict[str, Any]:
            event = self._google.events_by_id.get(eventId)
            if event is None:
                raise _error(404)
            return {"id": eventId, "status": event["status"]}

        self._run = run
        return self

    def execute(self) -> dict[str, Any]:
        result: dict[str, Any] = self._run()
        return result


def _error(status: int) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason="test"), b"")


def test_google_as_m17_assumes_passes_every_check() -> None:
    google = Google()

    checks = probe(google, "test-calendar", now=NOW)

    assert [check.passed for check in checks] == [True, True, True]
    assert google.calendars == {"test-calendar"}
    [event] = google.events_by_id.values()
    assert event["status"] == "cancelled"  # deleted again
    assert set(event["id"]) <= set("0123456789abcdefghijklmnopqrstuv")


def test_a_calendar_that_forgets_deleted_events_fails_the_probe() -> None:
    """A re-drive could then recreate an event the owner removed. The event
    the last insert made is deleted again."""
    google = Google(keeps_deleted=False)

    checks = probe(google, "test-calendar", now=NOW)

    assert [check.passed for check in checks] == [True, False, False]
    assert google.events_by_id == {}


def test_an_id_not_seen_as_taken_fails_and_nothing_is_left_live() -> None:
    google = Google(sees_taken_ids=False)

    checks = probe(google, "test-calendar", now=NOW)

    assert [check.passed for check in checks] == [False, True, False]
    assert all(event["status"] == "cancelled" for event in google.events_by_id.values())


def test_each_run_uses_a_fresh_id() -> None:
    google = Google()
    probe(google, "test-calendar", now=NOW)
    probe(google, "test-calendar", now=NOW)

    assert len(google.events_by_id) == 2


def test_a_first_insert_whose_reply_was_lost_is_cleaned_up() -> None:
    """The event was made though the call failed: it is deleted again."""
    google = Google(loses_first_reply=True)

    with pytest.raises(HttpError):
        probe(google, "test-calendar", now=NOW)

    [event] = google.events_by_id.values()
    assert event["status"] == "cancelled"
