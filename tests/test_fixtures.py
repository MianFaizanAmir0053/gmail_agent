"""Guards on the golden set itself.

A fixture with an internally inconsistent label is worse than no fixture: it
silently caps the achievable score and sends you debugging a model that is
right.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from app.eval.dataset import Fixture, load_fixtures

FIXTURES = load_fixtures()


def test_fixtures_exist() -> None:
    assert len(FIXTURES) >= 10


def test_ids_are_unique_and_sorted() -> None:
    ids = [f.id for f in FIXTURES]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_every_fixture_is_tagged(fixture: Fixture) -> None:
    assert fixture.tags, f"{fixture.id} has no tags"
    assert ("meeting" in fixture.tags) ^ ("non-meeting" in fixture.tags)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_tag_agrees_with_label(fixture: Fixture) -> None:
    assert fixture.expected.is_meeting == ("meeting" in fixture.tags)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_timestamps_are_timezone_aware(fixture: Fixture) -> None:
    assert fixture.now_utc.tzinfo is not None
    assert fixture.email.received_at.tzinfo is not None


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_meeting_fixtures_are_fully_specified(fixture: Fixture) -> None:
    expected = fixture.expected
    if not expected.is_meeting:
        return
    assert expected.title
    assert expected.start_utc is not None
    assert expected.end_utc is not None
    assert expected.timezone, "IANA zone is required -- offsets lose DST"
    assert expected.start_utc < expected.end_utc


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_non_meeting_fixtures_carry_no_event_data(fixture: Fixture) -> None:
    expected = fixture.expected
    if expected.is_meeting:
        return
    assert expected.start_utc is None
    assert expected.end_utc is None
    assert expected.attendees == []


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_events_are_in_the_future_relative_to_now(fixture: Fixture) -> None:
    """A fixture whose meeting predates its own grounding time is mislabelled."""
    if fixture.expected.start_utc is None:
        return
    assert fixture.expected.start_utc.astimezone(UTC) >= fixture.now_utc.astimezone(UTC)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_owner_is_never_an_attendee(fixture: Fixture) -> None:
    """Convention: attendees are the *other* participants."""
    assert "me@example.com" not in {a.lower() for a in fixture.expected.attendees}


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_no_real_looking_addresses_leaked(fixture: Fixture) -> None:
    blob = fixture.model_dump_json().lower()
    assert "@gmail.com" not in blob
    assert "@group.calendar.google.com" not in blob


def test_both_classes_are_represented() -> None:
    meetings = sum(f.expected.is_meeting for f in FIXTURES)
    assert 0 < meetings < len(FIXTURES)
