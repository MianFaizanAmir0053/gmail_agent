"""The planner's reads and helpers, on fakes (M19, D3)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from fake_gmail import SECRET, FakeMailbox, http_error

from app.config import WorkingHours
from app.google.calendar import BusyInterval
from app.google.gmail import SEARCH_HEADERS, GmailClient, MessageMeta
from app.planner.ops import (
    NO_CONTACT,
    SEVERAL_CONTACTS,
    FoundMessage,
    Unclear,
    find_contact,
    first_free,
    search,
    search_query,
)
from app.planner.plan import Search, Slot

KARACHI = ZoneInfo("Asia/Karachi")
"""Five hours ahead of UTC all year."""

HOURS = WorkingHours.parse("Mon-Fri 09:00-18:00")

GMAIL_PASS = "mx.google.com; dkim=pass; spf=pass; dmarc=pass (p=NONE) header.from=example.com"


def _search(**fields: object) -> Search:
    base: dict[str, object] = {
        "sender": None,
        "recipient": None,
        "words": None,
        "after": None,
        "before": None,
        "limit": 5,
    }
    return Search(**(base | fields))  # type: ignore[arg-type]


def _local(day: int, hour: int, minute: int = 0) -> datetime:
    """A time in Karachi in the week of Monday 2026-10-12, as UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=KARACHI).astimezone(UTC)


# --- the query: no Gmail syntax from the planner -----------------------------


def test_the_query_is_built_from_typed_fields() -> None:
    step = _search(
        sender="Sara Khan",
        recipient="me@example.com",
        words="budget review",
        after=datetime(2026, 10, 1, tzinfo=UTC),
        before=datetime(2026, 10, 3, tzinfo=UTC),
    )
    assert search_query(step) == (
        'from:"Sara Khan" to:"me@example.com" "budget" "review" -in:drafts -in:chats '
        f"after:{int(datetime(2026, 10, 1, tzinfo=UTC).timestamp()) - 1} "
        f"before:{int(datetime(2026, 10, 3, tzinfo=UTC).timestamp())}"
    )


def test_a_search_with_no_fields_lists_the_newest_mail() -> None:
    """Unsent drafts and chats are left out of every search, as the sync leaves them."""
    assert search_query(_search()) == "-in:drafts -in:chats"


@pytest.mark.parametrize(
    "words",
    [
        "x OR from:boss in:anywhere",
        'a" OR in:anywhere "b',
        "-label:secret {deal}",
    ],
)
def test_whatever_the_planner_wrote_stays_text(words: str) -> None:
    """Every value is in quotes and holds none, so Gmail reads none of it as an
    operator (D3): outside the quotes there are only the operators code wrote."""
    query = search_query(_search(sender=words, words=words))
    outside = re.sub(r'"[^"]*"', "", query).split()
    assert outside == ["from:", "-in:drafts", "-in:chats"]
    assert query.count('"') % 2 == 0


def test_a_value_that_is_only_quote_marks_is_left_out() -> None:
    assert search_query(_search(sender='""', words='" "')) == "-in:drafts -in:chats"


# --- the search: refs, labels and redacted subjects --------------------------


def _mailbox() -> FakeMailbox:
    box = FakeMailbox()
    box.put(
        "m1",
        labels={"INBOX"},
        at=datetime(2026, 10, 1, 9, tzinfo=UTC),
        sender="Sara Khan <sara@example.com>",
        Subject="Lunch on Friday?",
        Authentication_Results=GMAIL_PASS,
    )
    box.put(
        "m2",
        labels={"INBOX"},
        at=datetime(2026, 10, 2, 9, tzinfo=UTC),
        sender='"sara@example.com" <Sara@Example.com>',
        Subject=f"Your sign in code is {SECRET}",
    )
    box.put("m3", labels={"SPAM"}, at=datetime(2026, 10, 3, 9, tzinfo=UTC), Subject="Prize")
    box.put("m4", labels={"INBOX"}, at=datetime(2026, 10, 4, 9, tzinfo=UTC), sender="x@y.org")
    return box


def _gmail(box: FakeMailbox) -> GmailClient:
    return GmailClient(box, sleep=lambda _: None)


def test_a_search_finds_the_newest_first_within_its_limit() -> None:
    found = search(_gmail(_mailbox()), _search(sender="sara", limit=1))
    assert [message.id for message in found] == ["m2"]


def test_each_message_comes_back_labelled_by_what_gmail_says() -> None:
    m2, m1 = search(_gmail(_mailbox()), _search(sender="sara"))

    assert m1 == FoundMessage(
        id="m1",
        thread_id="t-m1",
        sender="sara@example.com",
        dmarc_passed=True,
        sent=False,
        received=datetime(2026, 10, 1, 9, tzinfo=UTC),
        subject="Lunch on Friday?",
    )
    # A display name is never who sent it, and no Gmail verdict is no pass.
    assert (m2.sender, m2.dmarc_passed) == ("sara@example.com", False)


def test_a_subject_loses_its_codes_and_links() -> None:
    """A metadata read cannot tell credential mail, so every subject is
    redacted as if it were (D3)."""
    m2, _ = search(_gmail(_mailbox()), _search(sender="sara"))
    assert SECRET not in m2.subject


def test_spam_and_trash_are_never_searched() -> None:
    found = search(_gmail(_mailbox()), _search(words="prize"))
    assert found == ()


def test_the_owners_own_mail_says_so() -> None:
    box = _mailbox()
    box.put("m5", labels={"SENT"}, at=datetime(2026, 10, 5, 9, tzinfo=UTC), sender="me@example.com")
    (found,) = search(_gmail(box), _search(sender="me@example.com"))
    assert found.sent is True


def test_a_message_gone_before_its_headers_are_read_is_left_out() -> None:
    box = _mailbox()
    box.fail("messages.get:m2", http_error(404))
    found = search(_gmail(box), _search(sender="sara"))
    assert [message.id for message in found] == ["m1"]


class _Binned:
    """A mailbox whose one listed message went to the bin before its read."""

    def search(self, query: str, limit: int = 200) -> list[str]:
        return ["m1"]

    def message_metadata(self, message_id: str, headers: tuple[str, ...] = ()) -> MessageMeta:
        return MessageMeta(
            id="m1",
            thread_id="t1",
            label_ids=frozenset({"TRASH"}),
            internal_date=datetime(2026, 10, 1, tzinfo=UTC),
            headers={"From": "sara@example.com"},
        )


def test_a_message_binned_after_the_listing_is_left_out() -> None:
    assert search(cast(GmailClient, _Binned()), _search()) == ()


def test_only_the_headers_a_search_needs_are_asked_for() -> None:
    box = _mailbox()
    search(_gmail(box), _search(sender="sara"))
    for asked in box.asked("messages.get"):
        assert asked["format"] == "metadata"
        assert asked["metadataHeaders"] == list(SEARCH_HEADERS)
        assert "snippet" not in asked["fields"]


# --- the first free slot -----------------------------------------------------


def _free(
    busy: list[BusyInterval],
    start: datetime,
    end: datetime,
    minutes: int = 30,
    *,
    now: datetime | None = None,
) -> Slot | None:
    return first_free(busy, start, end, minutes, hours=HOURS, zone=KARACHI, now=now or _local(1, 0))


def _busy(start: datetime, end: datetime) -> BusyInterval:
    return BusyInterval(start=start, end=end)


def test_a_free_morning_offers_nine_oclock() -> None:
    assert _free([], _local(12, 0), _local(13, 0)) == Slot(_local(12, 9), _local(12, 9, 30))


def test_busy_time_is_stepped_over_to_the_next_quarter_hour() -> None:
    busy = [_busy(_local(12, 9), _local(12, 10, 5)), _busy(_local(12, 10, 20), _local(12, 11))]
    assert _free(busy, _local(12, 0), _local(13, 0)) == Slot(_local(12, 11), _local(12, 11, 30))


def test_a_gap_too_short_is_passed_over() -> None:
    busy = [_busy(_local(12, 9), _local(12, 10)), _busy(_local(12, 10, 15), _local(12, 12))]
    assert _free(busy, _local(12, 0), _local(13, 0)) == Slot(_local(12, 12), _local(12, 12, 30))


def test_the_weekend_is_not_working_time() -> None:
    """Saturday 17 October, then Monday 19 October."""
    assert _free([], _local(17, 0), _local(20, 0)) == Slot(_local(19, 9), _local(19, 9, 30))


def test_a_slot_ends_by_the_end_of_the_working_day() -> None:
    busy = [_busy(_local(12, 9), _local(12, 17, 45))]
    assert _free(busy, _local(12, 0), _local(14, 0)) == Slot(_local(13, 9), _local(13, 9, 30))


def test_a_slot_stays_inside_the_range() -> None:
    assert _free([], _local(12, 14), _local(12, 14, 20)) is None
    assert _free([], _local(12, 14, 10), _local(12, 16)) == Slot(
        _local(12, 14, 15), _local(12, 14, 45)
    )


def test_a_slot_may_fill_the_range_exactly() -> None:
    assert _free([], _local(12, 14), _local(12, 14, 30)) == Slot(_local(12, 14), _local(12, 14, 30))


def test_after_busy_time_a_slot_starts_on_the_quarter_hour() -> None:
    busy = [_busy(_local(12, 9), _local(12, 10, 5))]
    assert _free(busy, _local(12, 0), _local(13, 0)) == Slot(_local(12, 10, 15), _local(12, 10, 45))


def test_a_slot_never_starts_in_the_past() -> None:
    now = _local(12, 13, 2)
    assert _free([], _local(12, 0), _local(13, 0), now=now) == Slot(
        _local(12, 13, 15), _local(12, 13, 45)
    )


def test_no_free_time_is_none() -> None:
    busy = [_busy(_local(12, 0), _local(13, 0))]
    assert _free(busy, _local(12, 0), _local(13, 0)) is None


def test_working_hours_are_the_owners_not_utc() -> None:
    slot = _free([], _local(12, 0), _local(13, 0))
    assert slot is not None and slot.start == datetime(2026, 10, 12, 4, tzinfo=UTC)


# --- a confirmed contact by name ---------------------------------------------

CONTACTS = ["sara.khan@example.com", "ali@example.org", "bilalahmed@gmail.com"]


@pytest.mark.parametrize(
    ("name", "address"),
    [
        ("Sara", "sara.khan@example.com"),
        ("sara khan", "sara.khan@example.com"),
        ("KHAN", "sara.khan@example.com"),
        ("Ali", "ali@example.org"),
        ("Bilal Ahmed", "bilalahmed@gmail.com"),
    ],
)
def test_one_confirmed_contact_matches(name: str, address: str) -> None:
    assert find_contact(name, CONTACTS) == address


def test_several_matches_ask_which() -> None:
    contacts = [*CONTACTS, "sara@example.net"]
    assert find_contact("Sara", contacts) == Unclear(SEVERAL_CONTACTS)


@pytest.mark.parametrize("name", ["Bilal", "Sar", "Zara", "...", "sara@example.com"])
def test_no_match_asks_for_the_address(name: str) -> None:
    """Only whole words match, and mail is never searched for an address (D3)."""
    assert find_contact(name, CONTACTS) == Unclear(NO_CONTACT)


# --- working hours -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "days", "start", "end"),
    [
        ("Mon-Fri 09:00-18:00", {0, 1, 2, 3, 4}, time(9), time(18)),
        ("sun-thu 08:30-17:00", {6, 0, 1, 2, 3}, time(8, 30), time(17)),
        ("Mon,Wed,Fri 10:00-16:00", {0, 2, 4}, time(10), time(16)),
        ("Sat 10:00-12:00", {5}, time(10), time(12)),
    ],
)
def test_working_hours_are_read(text: str, days: set[int], start: time, end: time) -> None:
    assert WorkingHours.parse(text) == WorkingHours(frozenset(days), start, end)


@pytest.mark.parametrize(
    "text", ["", "Mon-Fri", "Funday 09:00-18:00", "Mon-Fri 18:00-09:00", "Mon-Fri 9-5"]
)
def test_working_hours_that_cannot_be_read_are_refused(text: str) -> None:
    with pytest.raises(ValueError, match="WORKING_HOURS"):
        WorkingHours.parse(text)


def test_a_draft_is_never_found() -> None:
    """Not mail anyone sent: as the newest "message to Sara" it would mislead."""
    box = _mailbox()
    box.put(
        "m6", labels={"DRAFT"}, at=datetime(2026, 10, 9, 9, tzinfo=UTC), sender="me@example.com"
    )
    found = search(_gmail(box), _search(sender="me@example.com"))
    assert [message.id for message in found] == []


def test_a_sender_that_is_not_a_plain_address_is_unknown() -> None:
    """A quoted local part can spell another address before the real domain."""
    box = FakeMailbox()
    box.put(
        "m1",
        labels={"INBOX"},
        at=datetime(2026, 10, 1, 9, tzinfo=UTC),
        sender='"boss@corp.example"@evil.example',
        Authentication_Results="mx.google.com; dmarc=pass header.from=evil.example",
    )
    (found,) = search(_gmail(box), _search())
    assert (found.sender, found.dmarc_passed) == ("", False)
