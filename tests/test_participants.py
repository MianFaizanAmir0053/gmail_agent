"""Who is in the thread (M17, D4): the rule an invite's guests are held to.
Where each guest came from (M18, D5), from the `guest-*` cases in
`data/injection/`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, get_args

import pytest
from gmail_payloads import gmail_response, load_cases

from app.contracts import EmailMessage
from app.google.gmail import to_email_message
from app.policy.participants import (
    SOURCE_WORDS,
    WARNINGS,
    Source,
    card_source,
    guest_key,
    named,
    outside,
    participants,
)

OWNER = "owner@example.com"


def _message(*headers: tuple[str, str], labels: tuple[str, ...] = ("INBOX",)) -> dict[str, Any]:
    return {
        "labelIds": list(labels),
        "payload": {"headers": [{"name": name, "value": value} for name, value in headers]},
    }


def _dmarc(domain: str, *, result: str = "pass", server: str = "mx.google.com") -> tuple[str, str]:
    return (
        "Authentication-Results",
        f"{server}; dkim=pass header.i=@{domain}; spf=pass smtp.mailfrom={domain};"
        f" dmarc={result} (p=NONE sp=NONE dis=NONE) header.from={domain}",
    )


def _thread(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"messages": list(messages)}


# --- the comparator -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Sara.Khan+work@gmail.com", "sarakhan@gmail.com"),
        ("sarakhan@googlemail.com", "sara.khan@gmail.com"),
        ("Ali@Example.com", "ali@example.com"),
    ],
)
def test_the_same_mailbox_compares_equal(a: str, b: str) -> None:
    assert guest_key(a) == guest_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("ali+sales@example.com", "ali@example.com"),  # elsewhere a +tag may be its own mailbox
        ("a.li@example.com", "ali@example.com"),  # and so may a dot
    ],
)
def test_outside_gmail_the_address_is_compared_exactly(a: str, b: str) -> None:
    assert guest_key(a) != guest_key(b)


# --- who is a participant ---------------------------------------------------------------


def test_everyone_the_owner_wrote_to_is_a_participant() -> None:
    """Gmail's SENT label, not the From header: a forged From adds nothing."""
    thread = _thread(
        _message(
            ("From", OWNER),
            ("To", "Sara <sara@example.com>"),
            ("Cc", "ali@example.org, Bo <bo@example.net>"),
            labels=("SENT",),
        )
    )

    assert participants(thread) == {"sara@example.com", "ali@example.org", "bo@example.net"}


def test_a_sender_counts_only_with_gmails_dmarc_pass_for_their_own_domain() -> None:
    thread = _thread(
        _message(("From", "Sara <sara@example.com>"), _dmarc("example.com"), ("To", OWNER)),
        _message(("From", "eve@example.org"), _dmarc("example.org", result="fail"), ("To", OWNER)),
        _message(("From", "mallory@example.net"), ("To", OWNER)),  # no result at all
    )

    assert participants(thread) == {"sara@example.com"}


def test_an_address_only_in_the_cc_of_mail_received_is_outside() -> None:
    """A sender writes the Cc as freely as the From."""
    thread = _thread(
        _message(
            ("From", "sara@example.com"),
            _dmarc("example.com"),
            ("To", OWNER),
            ("Cc", "stranger@elsewhere.com"),
        )
    )

    assert "stranger@elsewhere.com" not in participants(thread)


def test_only_gmails_own_topmost_result_is_trusted() -> None:
    """A sender can add an Authentication-Results header of its own; Gmail's
    is the topmost, and it is the only one read."""
    forged_below = _thread(
        _message(
            ("From", "sara@example.com"),
            _dmarc("example.com", result="fail"),
            _dmarc("example.com"),  # lower down: written by the sender
            ("To", OWNER),
        )
    )
    forged_on_top = _thread(
        _message(
            ("From", "sara@example.com"),
            _dmarc("example.com", server="mx.example.com"),
            ("To", OWNER),
        )
    )

    assert participants(forged_below) == frozenset()
    assert participants(forged_on_top) == frozenset()


def test_a_pass_for_another_domain_does_not_count() -> None:
    thread = _thread(_message(("From", "sara@example.com"), _dmarc("lookalike.com"), ("To", OWNER)))

    assert participants(thread) == frozenset()


@pytest.mark.parametrize("label", ["SPAM", "TRASH"])
def test_mail_in_spam_or_trash_adds_no_one(label: str) -> None:
    thread = _thread(
        _message(("From", OWNER), ("To", "sara@example.com"), labels=("SENT", label)),
        _message(("From", "ali@example.com"), _dmarc("example.com"), labels=(label,)),
    )

    assert participants(thread) == frozenset()


def test_participants_are_compared_the_way_gmail_reads_them() -> None:
    thread = _thread(
        _message(("From", OWNER), ("To", "Sara.Khan+x@googlemail.com"), labels=("SENT",))
    )

    assert participants(thread) == {"sarakhan@gmail.com"}


# --- who is outside ---------------------------------------------------------------------------


def test_a_guest_is_outside_unless_a_participant_or_a_confirmed_contact() -> None:
    found = outside(
        ["Sara@Example.com", "ali@example.org", "new@example.net"],
        participants=frozenset({"sara@example.com"}),
        confirmed=frozenset({"ali@example.org"}),
    )

    assert found == ["new@example.net"]


# --- reading the thread -------------------------------------------------------------------


class _Threads:
    def __init__(self, status: int | None = None) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def get(self, **kwargs: Any) -> _Threads:
        self.calls.append(kwargs)
        return self

    def execute(self) -> dict[str, Any]:
        if self.status is not None:
            from types import SimpleNamespace

            from googleapiclient.errors import HttpError

            raise HttpError(SimpleNamespace(status=self.status, reason="test"), b"")
        return {
            "messages": [_message(("From", OWNER), ("To", "sara@example.com"), labels=("SENT",))]
        }


class _Service:
    def __init__(self, threads: _Threads) -> None:
        self._threads = threads

    def users(self) -> _Service:
        return self

    def threads(self) -> _Threads:
        return self._threads


def _gmail(threads: _Threads) -> Any:
    from app.google.gmail import GmailClient

    class Free:
        def spend(
            self, method: str, *, share: str | None = None, wait_for: float | None = None
        ) -> None:
            pass

    return GmailClient(_Service(threads), pacer=Free(), retry_for=0)


def test_the_thread_is_read_as_headers_and_labels_only() -> None:
    """No body, and a field mask that leaves out the snippet (D4)."""
    from app.google.gmail import GUEST_FIELDS, GUEST_HEADERS

    threads = _Threads()

    thread = _gmail(threads).thread_headers("t1")

    [call] = threads.calls
    assert (call["id"], call["format"], call["fields"]) == ("t1", "metadata", GUEST_FIELDS)
    assert list(call["metadataHeaders"]) == list(GUEST_HEADERS)
    assert participants(thread) == {"sara@example.com"}


def test_a_thread_gmail_no_longer_has_reads_as_empty() -> None:
    """Every guest is then outside, until the owner allows them."""
    assert _gmail(_Threads(status=404)).thread_headers("t1") == {"messages": []}


def test_any_other_failure_to_read_the_thread_is_raised() -> None:
    from googleapiclient.errors import HttpError

    with pytest.raises(HttpError):
        _gmail(_Threads(status=403)).thread_headers("t1")


# --- what a sender can write into the header (review, 2026-10-02) ------------------------


def _header(value: str) -> tuple[str, str]:
    return ("Authentication-Results", value)


@pytest.mark.parametrize(
    "value",
    [
        # A quoted envelope address hiding a result of its own, before Gmail's.
        'mx.google.com; spf=pass smtp.mailfrom="x;dmarc=pass header.from=corp.com y"@evil.com;'
        " dmarc=fail (p=NONE) header.from=corp.com",
        # A comment doing the same.
        "mx.google.com; spf=pass (sender says; dmarc=pass header.from=corp.com)"
        " smtp.mailfrom=evil.com; dmarc=fail header.from=corp.com",
        # Two results: one is not Gmail's, so neither is believed.
        "mx.google.com; dmarc=pass header.from=corp.com; dmarc=fail header.from=corp.com",
    ],
)
def test_a_sender_cannot_write_a_pass_of_their_own(value: str) -> None:
    thread = _thread(_message(("From", "ceo@corp.com"), _header(value), ("To", OWNER)))

    assert participants(thread) == frozenset()


def test_gmails_own_pass_is_read_through_comments_and_folding() -> None:
    value = (
        "mx.google.com;\r\n       dkim=pass header.i=@corp.com header.s=k1"
        " (a comment; with semicolons);"
        "\r\n       spf=pass (google.com: domain of ceo@corp.com) smtp.mailfrom=ceo@corp.com;"
        "\r\n       dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=corp.com"
    )
    thread = _thread(_message(("From", "CEO <ceo@corp.com>"), _header(value), ("To", OWNER)))

    assert participants(thread) == {"ceo@corp.com"}


def test_a_from_naming_two_people_counts_neither() -> None:
    thread = _thread(
        _message(("From", "a@corp.com, b@corp.com"), _dmarc("corp.com"), ("To", OWNER))
    )

    assert participants(thread) == frozenset()


def test_one_unreadable_recipient_header_drops_only_itself() -> None:
    """Mail the owner sent to `undisclosed-recipients:;` still counts its Cc."""
    thread = _thread(
        _message(
            ("From", OWNER),
            ("To", "undisclosed-recipients:;"),
            ("Cc", "sara@example.com, ali@example.org"),
            labels=("SENT",),
        )
    )

    assert participants(thread) == {"sara@example.com", "ali@example.org"}


def test_a_byte_order_mark_is_trimmed_as_the_web_app_trims_it() -> None:
    assert guest_key("\ufeffSara@Example.com ") == "sara@example.com"


# --- where a guest came from (M18, D5) ---------------------------------------------------


GUESTS = load_cases("guest")


def _card(case: dict[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    """The payload a park stores for the case, from the real preparation, and
    the guest keys the owner has allowed."""
    email = to_email_message(gmail_response(case))
    sources, quoted = named(case["guests"], email)
    thread = frozenset(guest_key(address) for address in case["participants"])
    outside_guests = outside(case["guests"], participants=thread, confirmed=frozenset())
    card = {
        "attendees": case["guests"],
        "outside_guests": outside_guests,
        "thread_guests": [guest for guest in case["guests"] if guest not in outside_guests],
        "guest_sources": sources,
        "quoted_section": quoted,
    }
    return card, frozenset(guest_key(address) for address in case["allowed"])


def test_every_source_has_a_case() -> None:
    expected = {source for case in GUESTS for source in case["expect"]["sources"].values()}
    assert expected == set(get_args(Source))


@pytest.mark.parametrize("case", GUESTS, ids=lambda case: case["id"])
def test_each_guest_gets_the_source_its_case_expects(case: dict[str, Any]) -> None:
    """A failure names the case and the guests' positions, never the email."""
    card, allowed = _card(case)
    wrong = [
        index
        for index, guest in enumerate(case["guests"])
        if card_source(guest, card, allowed) != case["expect"]["sources"][guest]
    ]
    assert not wrong, f"{case['id']}: guests {wrong} have the wrong source"
    assert card["quoted_section"] is case["expect"]["quoted_section"], case["id"]


def _email(**fields: Any) -> EmailMessage:
    values: dict[str, Any] = {
        "id": "m1",
        "thread_id": "t1",
        "subject": "Sync",
        "body_text": "Thursday at 3pm.",
        "sender": "sender@example.com",
        "recipients": ["owner@example.com"],
        "received_at": datetime(2026, 10, 1, tzinfo=UTC),
    }
    return EmailMessage(**(values | fields))


def test_the_subject_and_the_recipients_are_the_emails_own_words() -> None:
    email = _email(subject="Sync with sub@example.net", recipients=["cc@example.net"])

    sources, quoted = named(["sub@example.net", "cc@example.net"], email)

    assert sources == {"sub@example.net": "email", "cc@example.net": "email"}
    assert quoted is False


def test_a_guest_named_both_above_and_in_a_quote_is_named_in_the_email() -> None:
    body = "Add both@example.net.\n\nOn Mon, Amir <amir@example.org> wrote:\n> both@example.net"
    assert named(["both@example.net"], _email(body_text=body))[0] == {"both@example.net": "email"}


def test_a_payload_from_before_m18_has_no_sources() -> None:
    card = {"attendees": ["sara@example.com"], "outside_guests": ["sara@example.com"]}
    assert card_source("sara@example.com", card, frozenset()) is None


def test_a_stored_source_that_is_not_one_reads_as_none() -> None:
    card = {"guest_sources": {"sara@example.com": "trusted"}}
    assert card_source("sara@example.com", card, frozenset()) is None


def _stored(where: str, *, in_thread: bool, outside_now: bool) -> dict[str, Any]:
    guest = "sara@example.com"
    return {
        "guest_sources": {guest: where},
        "thread_guests": [guest] if in_thread else [],
        "outside_guests": [guest] if outside_now else [],
    }


def test_a_guest_in_the_thread_at_park_but_found_outside_since_is_not_in_it() -> None:
    """A check before a Confirm read the thread again, and found them outside
    it: the email deleted, say. The card says where the email named them."""
    card = _stored("email", in_thread=True, outside_now=True)
    assert card_source("sara@example.com", card, frozenset()) == "email"


def test_an_allowed_guest_stays_allowed_once_a_check_no_longer_marks_them() -> None:
    """A check before a Confirm leaves allowed guests out of `outside_guests`."""
    card = _stored("quoted", in_thread=False, outside_now=False)
    assert card_source("sara@example.com", card, frozenset({"sara@example.com"})) == "allowed"


def test_the_first_source_that_holds_is_shown() -> None:
    """In D5's order: in the thread, then allowed, then where the email names
    them. A guest the owner allowed is no warning, wherever the email put them."""
    guest = frozenset({"sara@example.com"})
    in_thread = _stored("absent", in_thread=True, outside_now=False)
    assert card_source("sara@example.com", in_thread, guest) == "thread"
    outside_it = _stored("absent", in_thread=False, outside_now=True)
    assert card_source("sara@example.com", outside_it, guest) == "allowed"
    assert card_source("sara@example.com", outside_it, frozenset()) == "absent"


def test_every_source_has_words_and_two_are_warnings() -> None:
    assert set(SOURCE_WORDS) == set(get_args(Source))
    assert {"quoted", "absent"} == WARNINGS


def test_the_web_card_says_the_same_words() -> None:
    """`dashboard/src/lib/guests.ts` mirrors the words and Telegram's note."""
    from pathlib import Path

    from app.telegram.cards import QUOTED_SECTION

    web = Path(__file__).resolve().parents[1] / "dashboard" / "src" / "lib" / "guests.ts"
    source = web.read_text("utf-8")
    assert all(f'{name}: "{words}"' in source for name, words in SOURCE_WORDS.items())
    assert f'"{QUOTED_SECTION}"' in source
