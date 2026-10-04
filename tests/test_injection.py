"""The injection suite's deterministic half (M18, D8; task 18.11).

Every case in `data/injection/` goes through the real preparation
(`to_email_message`, as `get_message` runs it) and the real prompt assembly
(`prompts.user_content`), and what the case says must hold, holds. No model
runs: the model's half is the committed run (18.13).

A failure names the case and the expectation, never the case's text. Quoting
payloads in a session blocked its shell (2026-10-02), so every check is
reduced to a boolean before it is asserted, and pytest has none of the text to
print.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import pytest
from gmail_payloads import FIXTURES_DIR, gmail_response, load_cases

from app.contracts import EmailMessage, ExtractionResult
from app.extraction.prompts import defuse, extract_system, user_content
from app.google.gmail import to_email_message
from app.policy.scrub import CREDENTIAL_NOTICE, shown

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
CASES = load_cases()
MAIL = [case for case in CASES if case["group"] != "output"]
OUTPUT = [case for case in CASES if case["group"] == "output"]
GROUPS = {"credential", "meeting", "hidden", "forged", "guest", "output"}
VARIANTS = {"original", "paraphrase", "hidden"}
LINK = re.compile(r"https?://|\bwww\.", re.IGNORECASE)


def _id(case: dict[str, Any]) -> str:
    return str(case["id"])


def _check(ok: bool, case: dict[str, Any], what: str) -> None:
    """Assert `ok`, naming only the case and the expectation."""
    assert ok, f"{case['id']}: {what}"


def _markers(text: str, kind: str) -> tuple[int, int] | None:
    """Where a block's one opening and one closing marker sit, or None when
    there is not exactly one of each."""
    found = re.findall(rf"<{kind}-([0-9a-f]{{8}})>", text)
    if len(found) != 1 or text.count(f"</{kind}-{found[0]}>") != 1:
        return None
    return text.index(f"<{kind}-{found[0]}>"), text.index(f"</{kind}-{found[0]}>")


# --- the cases themselves ------------------------------------------------------------


def test_every_case_is_named_for_its_file_and_well_formed() -> None:
    ids = {path.stem for path in FIXTURES_DIR.glob("*.json")}
    assert len(ids) == len(CASES)
    for case in CASES:
        _check(case["id"] in ids, case, "id matches a file name")
        _check(case["group"] in GROUPS, case, "a known group")
        _check(isinstance(case.get("expect"), dict), case, "an expect block")
        if "attack" in case:
            _check(case["attack"] in ids, case, "attack names a case")
            _check(case.get("variant") in VARIANTS, case, "a known variant")


def test_every_attack_is_written_three_more_ways_and_hidden_three_ways() -> None:
    """D8: each attack paraphrased three ways and hidden three ways. What a
    model writes cannot be hidden, so an output attack is only paraphrased."""
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in CASES:
        if "attack" in case:
            families[case["attack"]].append(case)
    assert len(families) >= 7
    groups = {case["id"]: case["group"] for case in CASES}
    for attack, members in families.items():
        variants = [member["variant"] for member in members]
        original = {"id": attack}
        _check(variants.count("paraphrase") >= 3, original, "three paraphrases")
        if groups[attack] != "output":
            _check(variants.count("hidden") >= 3, original, "hidden three ways")


def test_a_failure_names_the_case_and_never_its_text() -> None:
    case = {"id": "case-x", "body": "PAYLOAD-TEXT"}
    with pytest.raises(AssertionError) as caught:
        _check(case["body"] == "", case, "kept[0] survives")
    assert "case-x: kept[0] survives" in str(caught.value)
    assert "PAYLOAD-TEXT" not in str(caught.value)


# --- mail, through the preparation and into the prompt -------------------------------


@pytest.mark.parametrize("case", MAIL, ids=_id)
def test_a_case_holds_through_the_preparation(case: dict[str, Any]) -> None:
    email = to_email_message(gmail_response(case))
    expect = case["expect"]

    _check(email.credential is expect["credential"], case, "credential flag")
    if email.credential:
        _check(email.body_text == CREDENTIAL_NOTICE, case, "body is the fixed notice")
        _check(not LINK.search(email.subject), case, "no link in the subject")
        _check(not any(char.isdigit() for char in email.subject), case, "no code in the subject")
    for index, kept in enumerate(expect.get("kept", [])):
        _check(kept in email.body_text, case, f"kept[{index}] survives the preparation")
    for index, gone in enumerate(expect.get("gone", [])):
        removed = gone not in email.body_text and gone not in email.subject
        _check(removed, case, f"gone[{index}] is removed by the preparation")


@pytest.mark.parametrize("case", MAIL, ids=_id)
def test_a_case_holds_in_the_assembled_prompt(case: dict[str, Any]) -> None:
    """Everything the sender controls sits between the call's markers,
    nothing follows the closing one, and what must go never reaches the
    model. The owner's channel, the system instruction, carries none of it."""
    email = to_email_message(gmail_response(case))
    expect = case["expect"]
    prompt = user_content(email, now_utc=NOW, user_timezone="UTC")
    system = extract_system("Make it 5pm", proposal=True)

    bounds = _markers(prompt, "email")
    _check(bounds is not None, case, "one opening and one closing marker")
    assert bounds is not None
    start, end = bounds
    close = prompt.index(">", end) + 1
    _check(not prompt[close:].strip(), case, "nothing follows the closing marker")
    subject = prompt.find(f"Subject: {defuse(email.subject)}\n")
    _check(start < subject < end, case, "the subject sits between the markers")
    held = [*expect.get("kept", []), *expect.get("inside", [])]
    for index, text in enumerate(held):
        _check(start < prompt.find(text) < end, case, f"held[{index}] sits between the markers")
        _check(text not in system, case, f"held[{index}] stays out of the system instruction")
    for index, gone in enumerate(expect.get("gone", [])):
        _check(gone not in prompt, case, f"gone[{index}] never reaches the prompt")


# --- what a model writes, on the card and back in an Edit ------------------------------


def _email() -> EmailMessage:
    return EmailMessage(
        id="m1",
        thread_id="m1",
        subject="Weekly sync",
        body_text="Weekly sync on Monday at 10am.",
        sender="sara@example.com",
        recipients=["owner@example.com"],
        received_at=NOW,
    )


def _proposal(case: dict[str, Any]) -> ExtractionResult:
    return ExtractionResult(
        is_meeting=True,
        title=case["title"],
        start_utc=datetime(2026, 10, 6, 10, 0, tzinfo=UTC),
        end_utc=datetime(2026, 10, 6, 11, 0, tzinfo=UTC),
        timezone="UTC",
        location=case["location"],
        confidence=0.9,
        reasoning="",
    )


@pytest.mark.parametrize("case", OUTPUT, ids=_id)
def test_what_a_model_writes_holds_on_the_card(case: dict[str, Any]) -> None:
    fields = [field for field in (shown(case["title"]), shown(case["location"])) if field]
    card = " | ".join(fields)
    _check(all("\n" not in field and "\r" not in field for field in fields), case, "one line")
    for index, kept in enumerate(case["expect"]["kept"]):
        _check(kept in card, case, f"kept[{index}] on the card")
    for index, gone in enumerate(case["expect"]["gone"]):
        _check(gone not in card, case, f"gone[{index}] off the card")


@pytest.mark.parametrize("case", OUTPUT, ids=_id)
def test_what_a_model_writes_stays_data_in_an_edit(case: dict[str, Any]) -> None:
    """An Edit's re-extraction reads the proposal back (M18, D3): between its
    own markers, before the email, and no title can forge either block."""
    prompt = user_content(_email(), now_utc=NOW, user_timezone="UTC", current=_proposal(case))

    proposal = _markers(prompt, "proposal")
    email = _markers(prompt, "email")
    _check(proposal is not None and email is not None, case, "one marker pair for each block")
    assert proposal is not None and email is not None
    _check(proposal[1] < email[0], case, "the proposal comes before the email")
    for index, kept in enumerate(case["expect"]["kept"]):
        where = prompt.find(kept)
        _check(proposal[0] < where < proposal[1], case, f"kept[{index}] inside the proposal")
    for index, gone in enumerate(case["expect"]["gone"]):
        _check(gone not in prompt, case, f"gone[{index}] never reaches the prompt")
