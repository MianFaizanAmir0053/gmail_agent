"""The scrubber (M18, D1 and D2): mail made safe for a model to read.

Fixtures live in `data/injection/` and are cited by id. Their text is never
quoted here: see that folder's README.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from app.policy.scrub import is_credential, normalise, scrub, scrub_counted, unwrap

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "data" / "injection"


def _load(group: str) -> list[dict[str, Any]]:
    cases = [json.loads(path.read_text("utf-8")) for path in sorted(FIXTURES_DIR.glob("*.json"))]
    return [case for case in cases if case["group"] == group]


CREDENTIAL = _load("credential")
MEETING = _load("meeting")


def test_the_fixture_groups_are_not_empty() -> None:
    assert len(CREDENTIAL) >= 10
    assert len(MEETING) >= 6


# --- normalisation -------------------------------------------------------------------


def test_normalising_folds_full_width_letters_and_removes_invisible_characters() -> None:
    assert normalise("\uff28\uff54\uff54\uff50\uff53") == "Https"
    assert normalise("5\u200b2\u200c9\u200d0\u20601\ufeff8") == "529018"


def test_normalising_turns_non_breaking_spaces_into_spaces() -> None:
    assert normalise("482\u00a0913 and 662\u202f017") == "482 913 and 662 017"


def test_normalising_removes_private_use_characters() -> None:
    assert normalise("a\ue000b\uf8ffc") == "abc"


def test_normalising_unifies_line_endings() -> None:
    assert normalise("one\r\ntwo\rthree") == "one\ntwo\nthree"


# --- credential mail -----------------------------------------------------------------


@pytest.mark.parametrize("case", CREDENTIAL, ids=lambda case: case["id"])
def test_every_credential_fixture_is_recognised(case: dict[str, Any]) -> None:
    assert is_credential(case["subject"], case["body"])


@pytest.mark.parametrize("case", MEETING, ids=lambda case: case["id"])
def test_no_meeting_fixture_is_taken_for_credential_mail(case: dict[str, Any]) -> None:
    assert not is_credential(case["subject"], case["body"])


@pytest.mark.parametrize(
    "phrase",
    [
        "verification code",
        "one-time code",
        "one time passcode",
        "one-time password",
        "sign-in code",
        "signin code",
        "login code",
        "log in link",
        "security code",
        "authentication code",
        "recovery code",
        "backup codes",
        "magic link",
        "reset your password",
        "password reset",
        "forgot your password",
        "temporary password",
        "two-factor",
        "2FA",
        "two-step verification",
        "multi-factor",
        "OTP",
    ],
)
def test_each_strong_phrase_flags_a_message_in_the_subject_or_the_body(phrase: str) -> None:
    assert is_credential(f"About your {phrase}", "Thanks.")
    assert is_credential("Hello", f"Here is the {phrase.upper()} you asked for.")


def test_a_phrase_split_across_a_line_break_still_counts() -> None:
    assert is_credential("", "Your one-time\ncode is below.")


def test_phrases_match_whole_words_only() -> None:
    assert not is_credential("", "The ZOTP project and a codebase review.")


@pytest.mark.parametrize(
    "text",
    ["Passcode: 123456", "Attendee PIN: 4821#", "Access code: 2345 678 9012", "Meeting password"],
)
def test_what_meeting_invites_say_is_not_a_strong_phrase(text: str) -> None:
    assert not is_credential("Invitation", text)


# --- codes ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "482913",
        "482 913",
        "482-913",
        "4829 1374",
        "482\u00a0913",
        "4\u200b8\u200b2\u200b9\u200b1\u200b3",
        "9ZcB5x",
    ],
)
def test_a_code_near_a_cue_is_removed_whatever_its_separators(code: str) -> None:
    text = scrub(f"Your code is {code}.")
    assert text == "Your code is [code removed]."


def test_a_code_on_its_own_line_within_three_non_empty_lines_of_the_cue_is_removed() -> None:
    text = scrub("Here is your passcode.\n\n\nSome text\n\nMore text\n\n\n482913\n\nThanks")
    assert "482913" not in text
    assert "[code removed]" in text


def test_a_code_four_non_empty_lines_from_any_cue_is_left_alone() -> None:
    text = scrub("Here is your passcode.\nOne\nTwo\nThree\n482913")
    assert text.endswith("482913")


def test_without_a_cue_nothing_is_removed() -> None:
    text = "Call 482913 or try 9ZcB5x."
    assert scrub(text) == text


@pytest.mark.parametrize(
    "kept",
    [
        "at 1430",
        "14:30",
        "1430 hrs",
        "2026",
        "2026-10-05",
        "05-10-2026",
        "10-05",
        "+1 646 558 8656",
        "+16465588656",
        "(021) 3456 7890",
        "0300 123 4567",
        "Room 1204",
        "room B1204",
        "12th floor",
        "Order #48213977",
        "order number 48213977",
        "booking reference is BK48213",
        "ticket no. 4821397",
        "ext. 4821",
        "3.14159",
        "v2.4.1",
        "14h30",
    ],
)
def test_times_years_dates_phones_rooms_and_order_numbers_survive_beside_a_cue(kept: str) -> None:
    text = f"Your code is ready. {kept}"
    assert scrub(text) == text


def test_an_email_address_is_never_taken_for_a_code() -> None:
    text = "Send the code to sam2026@example.com or ali.k99@example.org."
    assert scrub(text) == text


def test_the_removed_marker_is_not_itself_a_cue() -> None:
    # The code three lines below the cue goes; the token six lines below stays.
    # Were the marker a cue, a second pass would reach the token from it.
    once = scrub("Your code is below.\nOne\nTwo\n482913\nThree\nFour\n771204")
    assert "482913" not in once
    assert once.endswith("771204")
    assert scrub(once) == once


# --- links ---------------------------------------------------------------------------


def test_an_ordinary_link_becomes_its_host() -> None:
    assert scrub("See https://www.Example.com/a/b?c=d#e.") == "See [link: example.com]."


def test_bare_www_and_host_path_forms_are_links_too() -> None:
    assert scrub("www.example.com and docs.example.org/x/y") == (
        "[link: example.com] and [link: docs.example.org]"
    )


def test_a_host_with_no_path_and_no_www_is_not_a_link() -> None:
    assert scrub("Write to example.com support.") == "Write to example.com support."


def test_an_allowlisted_meeting_link_keeps_its_host_and_path_and_loses_its_passcode() -> None:
    text = scrub("Join https://us02web.zoom.us/j/84512345678?pwd=abc123&uname=sam#success now")
    assert text == "Join https://us02web.zoom.us/j/84512345678 now"


def test_webex_keeps_only_its_meeting_id() -> None:
    text = scrub("https://acme.webex.com/acme/j.php?MTID=m1a2b3&tk=secret&RT=x")
    assert text == "https://acme.webex.com/acme/j.php?MTID=m1a2b3"


def test_teams_drops_the_passcode_of_its_short_links() -> None:
    assert scrub("https://teams.live.com/meet/9312845967041?p=Xy7kP2q") == (
        "https://teams.live.com/meet/9312845967041"
    )


def test_a_meet_link_without_a_scheme_is_kept_with_https() -> None:
    assert scrub("meet.google.com/abc-defg-hij") == "https://meet.google.com/abc-defg-hij"


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://zoom.us.evil.example/j/1", "zoom.us.evil.example"),
        ("https://evilzoom.us/j/1", "evilzoom.us"),
        ("https://meet.google.com.example/abc", "meet.google.com.example"),
    ],
)
def test_a_meeting_host_matches_exactly_or_at_a_dot_boundary(url: str, host: str) -> None:
    assert scrub(url) == f"[link: {host}]"


def test_a_sign_in_path_on_a_meeting_host_is_not_kept() -> None:
    assert scrub("https://zoom.us/reset_password/abc123") == "[link: zoom.us]"


def test_a_non_ascii_host_is_shown_as_punycode() -> None:
    text = scrub("https://z\u043e\u043em.us/j/1")
    assert text.startswith("[link: xn--")
    assert text.endswith(".us]")


@pytest.mark.parametrize(
    "text",
    [
        "hxxps://evil.example/x",
        "https://evil[.]example/x",
        "evil[.]example/reset?t=1",
        "\uff48\uff54\uff54\uff50\uff53://evil.example/x",
        "https://evil.ex\u200bample/x",
    ],
)
def test_an_obfuscated_link_is_found(text: str) -> None:
    assert scrub(text) == "[link: evil.example]"


def test_a_link_keeps_the_punctuation_that_follows_it() -> None:
    assert scrub("(see https://example.com/a), then") == "(see [link: example.com]), then"


def test_a_link_with_parentheses_keeps_them() -> None:
    assert scrub("https://en.example.org/wiki/A_(B)") == "[link: en.example.org]"


# --- wrappers ------------------------------------------------------------------------


def test_safe_links_are_unwrapped() -> None:
    inner = "https%3A%2F%2Fus02web.zoom.us%2Fj%2F84512345678%3Fpwd%3Dabc"
    wrapped = f"https://nam12.safelinks.protection.outlook.com/?url={inner}&data=05%7C01&reserved=0"
    assert scrub(wrapped) == "https://us02web.zoom.us/j/84512345678"


def test_proofpoint_v2_is_unwrapped() -> None:
    wrapped = (
        "https://urldefense.proofpoint.com/v2/url?u=https-3A__evil.example_reset-3Ft-3D1&d=DwMF"
    )
    assert unwrap(wrapped) == ("https://evil.example/reset?t=1", True)
    assert scrub(wrapped) == "[link: evil.example]"


def test_proofpoint_v3_is_unwrapped_with_its_replaced_characters() -> None:
    # "!" in the inner URL is carried as "*" plus the base64url of "!".
    wrapped = "https://urldefense.com/v3/__https://evil.example/a*b__;IQ!!AbCd$"
    assert unwrap(wrapped) == ("https://evil.example/a!b", True)


def test_mimecast_gives_only_its_domain() -> None:
    wrapped = "https://protect-eu.mimecast.com/s/AbCdEfGh?domain=zoom.us"
    assert scrub(wrapped) == "[link: zoom.us]"


def test_a_google_redirect_is_unwrapped() -> None:
    wrapped = "https://www.google.com/url?q=https://evil.example/x&sa=D"
    assert scrub(wrapped) == "[link: evil.example]"


# --- links before codes ---------------------------------------------------------------


def test_a_meeting_links_path_is_never_read_as_a_code() -> None:
    text = scrub("Passcode below\nhttps://zoom.us/j/84512345678\nPasscode: 482913")
    assert "https://zoom.us/j/84512345678" in text
    assert "482913" not in text


def test_a_hosts_digits_are_never_read_as_codes() -> None:
    assert scrub("Code at https://us02web.example/abc") == "Code at [link: us02web.example]"


# --- the whole scrub -------------------------------------------------------------------


@pytest.mark.parametrize("case", MEETING, ids=lambda case: case["id"])
def test_meeting_mail_keeps_what_it_must_and_loses_what_it_must(case: dict[str, Any]) -> None:
    text = scrub(case["body"])
    for kept in case["expect"]["kept"]:
        assert kept in text, kept
    for gone in case["expect"]["gone"]:
        assert gone not in text, gone


@pytest.mark.parametrize("case", MEETING + CREDENTIAL, ids=lambda case: case["id"])
def test_scrubbing_twice_changes_nothing(case: dict[str, Any]) -> None:
    once = scrub(case["body"])
    assert scrub(once) == once


def test_thousands_of_links_all_come_back_intact() -> None:
    # More placeholders than one private-use character per link could number.
    links = [f"https://zoom.us/j/{index}" for index in range(7000)]
    text = scrub("Code: 482913\n" + "\n".join(links))
    assert text.split("\n") == ["Code: [code removed]", *links]


def test_the_counts_are_returned_by_kind() -> None:
    result = scrub_counted(
        "Code: 482913\nhttps://example.com/x and https://zoom.us/j/1?pwd=a and https://b.example/y"
    )
    assert (result.links, result.meeting_links, result.codes) == (2, 1, 1)


def test_the_log_carries_counts_never_the_removed_text(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.policy.scrub"):
        scrub("Code: 482913 at https://secret.example/tok3n")
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "1 code" in logged and "1 link" in logged
    assert "482913" not in logged
    assert "secret.example" not in logged
    assert "tok3n" not in logged


def test_a_clean_text_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.policy.scrub"):
        scrub("See you at 3pm in Room 4.")
    assert caplog.records == []
