"""The keyed hash an approval binds (M17, D2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from gmail_payloads import load_cases

from app.contracts import ExtractionResult
from app.policy.hashing import (
    HOLD,
    INVITE,
    Binding,
    args_hash,
    args_key,
    bound,
    event_args,
    shown,
    tool_for,
)
from app.tools.calendar_tool import CreateEventInput

KEY = args_key("test-fernet-key-for-hashing-only")
CALENDAR = "test-calendar@group.calendar.google.com"
START = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)


def _args(**overrides: object) -> CreateEventInput:
    fields: dict[str, object] = {
        "title": "Design review",
        "start_utc": START,
        "end_utc": START + timedelta(hours=1),
        "timezone": "Asia/Karachi",
        "attendees": ["sara@example.com", "ali@example.com"],
        "location": "Room 4B",
        "description": "Created by mailagent from message m1.",
    }
    fields.update(overrides)
    return CreateEventInput.model_validate(fields)


def _hash(args: CreateEventInput, *, key: bytes = KEY, calendar: str = CALENDAR) -> str:
    return args_hash(key, tool=tool_for(args.attendees), calendar_id=calendar, args=args)


def test_the_same_event_written_differently_hashes_the_same() -> None:
    """Guest order and case, and the zone a time is written in, are not
    differences the owner could see on the card."""
    karachi = timezone(timedelta(hours=5))
    a = _args()
    b = _args(
        attendees=["ALI@example.com", "sara@example.com", "sara@example.com"],
        start_utc=START.astimezone(karachi),
        end_utc=(START + timedelta(hours=1)).astimezone(karachi),
    )
    assert _hash(a) == _hash(b)


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Design review (moved)"},
        {"start_utc": START + timedelta(minutes=30)},
        {"end_utc": START + timedelta(hours=2)},
        {"attendees": ["sara@example.com", "ali@example.com", "eve@example.com"]},
        {"location": None},
        {"description": "Created by mailagent from message m2."},
        {"timezone": "Europe/London"},
    ],
)
def test_any_change_the_provider_would_see_changes_the_hash(change: dict[str, object]) -> None:
    assert _hash(_args(**change)) != _hash(_args())


def test_the_calendar_is_part_of_what_is_approved() -> None:
    """Approved for one calendar, a re-drive after a switch must not book on
    another, where the first event cannot be found."""
    assert _hash(_args(), calendar="other@group.calendar.google.com") != _hash(_args())


def test_the_hash_is_keyed() -> None:
    """Kept for good, an unkeyed hash would confirm a guessed title and time."""
    assert _hash(_args(), key=args_key("another-key")) != _hash(_args())


def test_a_hold_has_no_guests_and_an_invite_has_some() -> None:
    assert tool_for([]) == HOLD
    assert tool_for(["sara@example.com"]) == INVITE


def test_the_hash_is_a_hex_digest() -> None:
    digest = _hash(_args())
    assert len(digest) == 64 and int(digest, 16) >= 0


PROPOSED: dict[str, object] = {
    "is_meeting": True,
    "title": "Design review",
    "start_utc": "2026-10-05T11:00:00Z",
    "end_utc": "2026-10-05T12:00:00Z",
    "timezone": "Asia/Karachi",
    "attendees": ["sara@example.com"],
    "location": None,
    "confidence": 0.9,
    "reasoning": "Sara asked to meet Monday",
}


def test_a_parked_payload_is_bound_as_act_would_run_it() -> None:
    """The row's hash is taken from the thread's payload, under the current
    code: it must equal the hash of what `act` would send."""
    binding = Binding(calendar_id=CALENDAR, key=KEY)
    tool, digest = bound("m1", PROPOSED, binding)

    extraction = ExtractionResult.model_validate(PROPOSED)
    args = event_args(extraction, "m1")
    assert tool == INVITE
    assert digest == args_hash(KEY, tool=INVITE, calendar_id=CALENDAR, args=args)


def test_a_payload_that_cannot_run_is_left_unbound() -> None:
    """No times, or not an extraction at all: there is nothing to approve,
    and a Confirm on it is refused as not ready."""
    binding = Binding(calendar_id=CALENDAR, key=KEY)
    assert bound("m1", {**PROPOSED, "start_utc": None}, binding) == (None, None)
    assert bound("m1", {"title": "only a title"}, binding) == (None, None)


def test_the_arguments_come_from_the_extraction_and_the_message() -> None:
    """One function builds them for the hash at park and for `act`, so the
    two can never drift apart."""
    extraction = ExtractionResult(
        is_meeting=True,
        title=None,
        start_utc=START,
        end_utc=START + timedelta(hours=1),
        timezone=None,
        attendees=["sara@example.com"],
        location=None,
        confidence=0.9,
        reasoning="r",
    )
    args = event_args(extraction, "m7")
    assert args.title == "(untitled)"
    assert args.timezone == "UTC"
    assert args.description == "Created by mailagent from message m7."
    assert args.attendees == ["sara@example.com"]


def _extraction(title: str | None, location: str | None) -> ExtractionResult:
    return ExtractionResult(
        is_meeting=True,
        title=title,
        start_utc=START,
        end_utc=START + timedelta(hours=1),
        timezone="Asia/Karachi",
        attendees=["sara@example.com"],
        location=location,
        confidence=0.9,
        reasoning="r",
    )


@pytest.mark.parametrize("case", load_cases("output"), ids=lambda case: case["id"])
def test_the_event_carries_the_title_and_location_scrubbed(case: dict[str, Any]) -> None:
    """What the model wrote, as the card shows it (M18, D6). A failure names
    the case and the expectation's index, never the text."""
    args = event_args(_extraction(case["title"], case["location"]), "m1")
    text = f"{args.title}\n{args.location}"
    lost = [index for index, kept in enumerate(case["expect"]["kept"]) if kept not in text]
    left = [index for index, gone in enumerate(case["expect"]["gone"]) if gone in text]
    assert not lost, f"{case['id']}: kept {lost} lost"
    assert not left, f"{case['id']}: gone {left} left"
    as_shown = (args.title, args.location) == (
        shown(case["title"]) or "(untitled)",
        shown(case["location"]),
    )
    assert as_shown, case["id"]


def test_a_payload_binds_the_same_hash_scrubbed_or_not() -> None:
    """A checkpoint made before titles were scrubbed binds the arguments its
    card shows now: the card and the hash cannot disagree (M18, D6)."""
    binding = Binding(calendar_id=CALENDAR, key=KEY)
    raw = {**PROPOSED, "title": "Kickoff, agenda at docs.example.net/q4", "location": "Room 4B\n"}
    clean = {**raw, "title": shown(raw["title"]), "location": shown(raw["location"])}

    assert clean["title"] == "Kickoff, agenda at [link: docs.example.net]"
    assert bound("m1", raw, binding) == bound("m1", clean, binding)


def test_a_field_with_nothing_left_is_none() -> None:
    assert shown(None) is None
    assert shown(" \n\t ") is None
    assert shown(42) is None
    args = event_args(_extraction(" \n ", " "), "m1")
    assert (args.title, args.location) == ("(untitled)", None)


def test_an_event_id_is_one_google_accepts_and_the_same_every_time() -> None:
    """Base32hex, 5-1024 characters: a later attempt asks for this id rather
    than booking again (M17, D3)."""
    from app.policy.hashing import event_id_for

    first = event_id_for(KEY, "m1", "a" * 64)
    assert first == event_id_for(KEY, "m1", "a" * 64)
    assert first.startswith("ma") and len(first) == 32
    assert set(first) <= set("0123456789abcdefghijklmnopqrstuv")
    assert event_id_for(KEY, "m2", "a" * 64) != first
    assert event_id_for(KEY, "m1", "b" * 64) != first
    assert event_id_for(args_key("another-key"), "m1", "a" * 64) != first
