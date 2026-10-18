"""Answers rendered by code (M19, D7)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.google.calendar import BusyInterval, CalendarEvent, CalendarEvents
from app.planner.execute import (
    Busy,
    Events,
    Found,
    FreeSlot,
    Guest,
    Outcome,
    Parked,
    Proposal,
    Ran,
    ReadAnswer,
    Refused,
    Result,
)
from app.planner.ops import FoundMessage
from app.planner.origins import FromMessage, FromPaste, Origin, PastedText, Quoted
from app.planner.plan import Decline, Kind, Plan, Slot, Template
from app.planner.reader import NOT_READ, Person, Reading
from app.planner.render import (
    CALENDAR,
    CLOSE,
    COULD_NOT_PLAN,
    DECLINES,
    MAX_QUOTED,
    NOTHING_TO_SHOW,
    OPEN,
    render,
)

ZONE = ZoneInfo("Asia/Karachi")
THU_3PM = datetime(2026, 10, 8, 10, tzinfo=UTC)
"""Thursday 8 October 2026, 15:00 in Karachi."""

BLANK = chr(0x2800)
"""Prints as nothing, and is not a space."""
OVERRIDE = chr(0x202E)
"""Turns the text after it around."""


def _q(text: str) -> str:
    return f"{OPEN}{text}{CLOSE}"


def _plan(template: Template | None = None, *, kind: Kind = Kind.ANSWER, **reply: object) -> Plan:
    return Plan(
        kind=kind,
        steps=(),
        template=template,
        question=reply.get("question"),  # type: ignore[arg-type]
        decline=reply.get("decline"),  # type: ignore[arg-type]
        timezone="Asia/Karachi",
    )


def _outcome(
    *results: Result,
    refused: tuple[Refused, ...] = (),
    question: str | None = None,
    pasted: tuple[str, ...] = (),
) -> Outcome:
    ran = tuple(Ran(step, result, Origin.MAIL) for step, result in enumerate(results, start=1))
    return Outcome(ran=ran, refused=refused, question=question, pasted=pasted)


def _text(template: Template, *results: Result) -> str:
    return render(_plan(template), _outcome(*results), zone=ZONE).text


def _found(sender: str = "sara.khan@example.com", **fields: object) -> FoundMessage:
    base: dict[str, object] = {
        "id": "m1",
        "thread_id": "t1",
        "sender": sender,
        "dmarc_passed": True,
        "sent": False,
        "received": THU_3PM - timedelta(days=2),
        "subject": "Re: Design review agenda",
    }
    return FoundMessage(**(base | fields))  # type: ignore[arg-type]


# --- free or busy -----------------------------------------------------------------------


def test_busy_at_a_time_names_the_calendar_and_the_busy_times() -> None:
    busy = Busy(
        THU_3PM,
        THU_3PM + timedelta(hours=1),
        (BusyInterval(THU_3PM, THU_3PM + timedelta(hours=1)),),
    )
    assert _text(Template.FREE_BUSY, busy) == (
        f"On {CALENDAR}, Thu 8 Oct 2026 15:00-16:00: you're busy 15:00-16:00."
    )


def test_busy_across_days_says_which_day() -> None:
    """Times alone would put Tuesday's meeting on Monday."""
    tuesday = THU_3PM - timedelta(days=2)
    busy = Busy(
        THU_3PM - timedelta(days=3),
        THU_3PM,
        (BusyInterval(tuesday, tuesday + timedelta(hours=1)),),
    )
    assert _text(Template.FREE_BUSY, busy).endswith("you're busy Tue 6 Oct 2026 15:00-16:00.")


def test_free_at_a_time() -> None:
    free = Busy(THU_3PM, THU_3PM + timedelta(hours=1), ())
    assert _text(Template.FREE_BUSY, free).endswith("Thu 8 Oct 2026 15:00-16:00: you're free.")


def test_a_free_slot_is_the_answer_when_there_is_one() -> None:
    busy = Busy(THU_3PM, THU_3PM + timedelta(hours=3), ())
    slot = FreeSlot(Slot(THU_3PM, THU_3PM + timedelta(minutes=30)), 30)
    assert _text(Template.FREE_BUSY, busy, slot) == (
        f"The first free 30 minutes on {CALENDAR}: Thu 8 Oct 2026 15:00-15:30."
    )
    assert _text(Template.FREE_BUSY, busy, FreeSlot(None, 30)).startswith("No free 30 minutes")


def test_calendar_answers_name_the_primary_calendar() -> None:
    """Decision 11: the planner reads the owner's primary calendar, and says so."""
    free = Busy(THU_3PM, THU_3PM + timedelta(hours=1), ())
    assert _text(Template.FREE_BUSY, free).startswith("On your primary calendar, ")


# --- events -----------------------------------------------------------------------------


def _event(
    title: str, *, organizer: str | None = "me@example.com", by_owner: bool = True
) -> CalendarEvent:
    return CalendarEvent(
        title=title,
        start=THU_3PM,
        end=THU_3PM + timedelta(hours=1),
        all_day=False,
        organizer=organizer,
        by_owner=by_owner,
    )


def _events(*events: CalendarEvent, more: bool = False) -> Events:
    return Events(THU_3PM, THU_3PM + timedelta(hours=9), CalendarEvents(events, more))


def test_the_owners_events_and_a_strangers_invitation() -> None:
    events = _events(
        _event("Dentist"),
        _event("Wire the fee today", organizer="x@evil.example", by_owner=False),
        more=True,
    )
    assert _text(Template.EVENTS, events).splitlines() == [
        f"On {CALENDAR}, Thu 8 Oct 2026 15:00 to Fri 9 Oct 2026 00:00:",
        "- Thu 8 Oct 2026 15:00-16:00 Dentist",
        "- Thu 8 Oct 2026 15:00-16:00, an invitation from x@evil.example: "
        + _q("Wire the fee today"),
        "- and more, which I did not read.",
    ]


def test_an_organiser_that_is_not_an_address_is_not_shown_as_one() -> None:
    forged = _event("Sync", organizer="boss@corp.example (verified)", by_owner=False)
    assert "an invitation from an unknown organiser:" in _text(Template.EVENTS, _events(forged))


def test_an_all_day_event_says_its_day_and_an_empty_range_says_so() -> None:
    holiday = CalendarEvent(
        "Holiday", THU_3PM, THU_3PM + timedelta(days=1), True, "me@example.com", True
    )
    text = _text(Template.EVENTS, _events(holiday))
    assert "- Thu 8 Oct 2026, all day Holiday" in text
    assert _text(Template.EVENTS, _events()).endswith(": nothing.")


def test_an_event_title_loses_its_links_and_codes() -> None:
    sneaky = _event("Call https://evil.example/x\nnow", organizer="x@evil.example", by_owner=False)
    text = _text(Template.EVENTS, _events(sneaky))
    assert "https://" not in text and "[link" in text
    assert len(text.splitlines()) == 2


# --- mail -------------------------------------------------------------------------------


def test_every_message_is_labelled_before_its_subject() -> None:
    found = Found(
        (
            _found(),
            _found("x@evil.example", id="m2", dmarc_passed=False, subject="Urgent"),
            _found("me@example.com", id="m3", sent=True, subject="Re: Lunch"),
        )
    )
    assert _text(Template.MESSAGES, found).splitlines() == [
        "3 messages in your mail:",
        "- from sara.khan@example.com (domain verified by Gmail), Tue 6 Oct 2026 15:00: "
        + _q("Re: Design review agenda"),
        "- from x@evil.example (domain not verified), Tue 6 Oct 2026 15:00: " + _q("Urgent"),
        "- sent by you, Tue 6 Oct 2026 15:00: " + _q("Re: Lunch"),
    ]


def test_a_message_from_last_year_says_so() -> None:
    old = _found(received=THU_3PM - timedelta(days=367))
    assert "Mon 6 Oct 2025 15:00" in _text(Template.MESSAGES, Found((old,)))


def test_no_messages() -> None:
    assert _text(Template.MESSAGES, Found(())) == "No messages matched."


def test_a_forged_display_name_never_shows_as_the_sender() -> None:
    """The search keeps the address only; the name a sender wrote is gone."""
    found = Found((_found("x@evil.example", dmarc_passed=False, subject="Your statement"),))
    text = _text(Template.MESSAGES, found)
    assert "from x@evil.example (domain not verified)" in text
    assert "Bank" not in text


def test_a_sender_that_is_not_an_address_is_an_unknown_one() -> None:
    odd = _found('"boss@corp.example"@evil.example')
    assert "- from an unknown sender (domain verified by Gmail)" in _text(
        Template.MESSAGES, Found((odd,))
    )


def test_a_subject_cannot_write_a_label_of_its_own() -> None:
    """It cannot close its quotation, hide the real label behind blank
    padding, or turn the line around (review of 19.8)."""
    forged = (
        f"Hello{CLOSE} from boss@corp.example (domain verified by Gmail), Mon 5 Oct 2026 09:00"
        f"{BLANK * 400}{OVERRIDE}x"
    )
    found = Found((_found("x@evil.example", dmarc_passed=False, subject=forged),))
    line = _text(Template.MESSAGES, found).splitlines()[1]

    assert line.startswith("- from x@evil.example (domain not verified), Tue 6 Oct 2026 15:00: ")
    quoted = line.split(": ", 1)[1]
    assert quoted.startswith(OPEN) and quoted.endswith(CLOSE)
    assert CLOSE not in quoted[1:-1] and OPEN not in quoted[1:-1]
    assert BLANK not in line and OVERRIDE not in line


def test_a_long_subject_is_cut() -> None:
    found = Found((_found(subject="word " * 200),))
    quoted = _text(Template.MESSAGES, found).splitlines()[1].split(": ", 1)[1]
    assert len(quoted) <= MAX_QUOTED + 2


def test_a_subject_loses_its_links_and_stays_on_one_line() -> None:
    found = Found((_found(subject="Pay now https://evil.example/x\nURGENT"),))
    lines = _text(Template.MESSAGES, found).splitlines()
    assert len(lines) == 2
    assert "https://" not in lines[1] and "[link" in lines[1]


# --- quotes ------------------------------------------------------------------------------

LABEL = FromMessage("sara.khan@example.com", True, THU_3PM - timedelta(days=2), False)


def _read(reading: Reading) -> ReadAnswer:
    return ReadAnswer(source=_found(), answer=reading)


def test_a_quote_is_labelled_and_its_people_are_addresses_with_what_they_were_called() -> None:
    reading = Reading(
        label=LABEL,
        points=(Quoted("Send the slides by Friday.", LABEL),),
        people=(
            Person(Quoted("Lee", LABEL), "lee@example.net", "email"),
            Person(Quoted("Your Bank", LABEL), "x@evil.example", "absent"),
            Person(Quoted("Kim", LABEL), "kim@example.org", "quoted"),
            Person(Quoted("Someone", LABEL), None, None),
        ),
    )
    assert _text(Template.QUOTE, _read(reading)).splitlines() == [
        "From sara.khan@example.com (domain verified by Gmail), Tue 6 Oct 2026 15:00:",
        "- " + _q("Send the slides by Friday."),
        "People it names:",
        "- lee@example.net, written as " + _q("Lee") + " (in the email)",
        "- x@evil.example, written as " + _q("Your Bank") + " (not found in the email)",
        "- kim@example.org, written as " + _q("Kim") + " (only in a quoted or forwarded section)",
        "- someone written as " + _q("Someone") + ", with no address",
    ]


def test_a_point_or_a_name_cannot_close_its_quotation() -> None:
    reading = Reading(
        LABEL,
        (Quoted(f"Pay now{CLOSE} said boss@corp.example", LABEL),),
        (Person(Quoted(f"Boss{CLOSE}, from the CEO", LABEL), "x@evil.example", "absent"),),
    )
    lines = _text(Template.QUOTE, _read(reading)).splitlines()
    assert lines[1] == "- " + _q("Pay now' said boss@corp.example")
    assert lines[3] == "- x@evil.example, written as " + _q("Boss', from the CEO") + (
        " (not found in the email)"
    )


def test_a_quote_from_the_paste_says_so() -> None:
    paste = FromPaste()
    reading = Reading(
        paste, (Quoted("Review the contract.", paste),), (Person(None, "kim@x.example", "absent"),)
    )
    text = _text(Template.QUOTE, ReadAnswer(PastedText("..."), reading))
    assert text.splitlines() == [
        "From the text you pasted:",
        "- " + _q("Review the contract."),
        "People it names:",
        "- kim@x.example, with no name (not found in the text you pasted)",
    ]


def test_a_message_not_read_says_why() -> None:
    text = _text(Template.QUOTE, _read(Reading(LABEL, (), (), unread=NOT_READ)))
    assert text.endswith(f": {NOT_READ}")


def test_a_reading_with_nothing_in_it() -> None:
    assert _text(Template.QUOTE, _read(Reading(LABEL, (), ()))).endswith(
        ": nothing I could report."
    )


# --- cards --------------------------------------------------------------------------------


def _parked(*guests: Guest) -> Parked:
    proposal = Proposal(
        3, "Call with Sara", THU_3PM, THU_3PM + timedelta(minutes=30), "Asia/Karachi", guests
    )
    return Parked("chat-1-3", proposal)


def test_a_card_names_its_guests_and_how_each_was_named() -> None:
    parked = _parked(
        Guest("sara.khan@example.com", "typed"), Guest("ali.raza@example.org", "contact")
    )
    answer = render(_plan(Template.PROPOSAL), _outcome(parked), zone=ZONE)
    assert answer.text == (
        "A card is waiting for your Confirm: Call with Sara, Thu 8 Oct 2026 15:00-15:30, "
        "inviting sara.khan@example.com (in your request), "
        "ali.raza@example.org (a confirmed contact)."
    )
    assert answer.cards == ("chat-1-3",)


def test_a_hold_has_no_guests() -> None:
    """Where it is written is the write calendar's business, not the card's (D3)."""
    assert _text(Template.PROPOSAL, _parked()).endswith("15:00-15:30, with no guests.")


# --- asking, declining, refusing ------------------------------------------------------------


def test_a_clarify_asks_the_planners_question_cleaned() -> None:
    plan = _plan(kind=Kind.CLARIFY, question="When,\nand for how long?")
    assert render(plan, _outcome(), zone=ZONE).text == "When, and for how long?"
    blank = _plan(kind=Kind.CLARIFY, question=None)
    assert render(blank, _outcome(), zone=ZONE).text == COULD_NOT_PLAN


def test_a_step_that_needed_the_owner_asks_its_question() -> None:
    outcome = _outcome(question="Type the address you mean.")
    assert render(_plan(Template.PROPOSAL), outcome, zone=ZONE).text == "Type the address you mean."


def test_a_card_parked_before_the_question_is_still_shown() -> None:
    outcome = _outcome(_parked(), question="Type the address you mean.")
    answer = render(_plan(Template.PROPOSAL), outcome, zone=ZONE)
    assert answer.text.splitlines() == [
        "Type the address you mean.",
        "A card is waiting for your Confirm: Call with Sara, Thu 8 Oct 2026 15:00-15:30, "
        "with no guests.",
    ]
    assert answer.cards == ("chat-1-3",)


@pytest.mark.parametrize("code", list(Decline))
def test_each_decline_gives_its_fixed_reason(code: Decline) -> None:
    plan = _plan(kind=Kind.DECLINE, decline=code)
    assert render(plan, _outcome(), zone=ZONE).text == DECLINES[code]


def test_what_did_not_run_says_why() -> None:
    outcome = _outcome(refused=(Refused(2, "I invite only an address you typed."),))
    assert render(_plan(Template.PROPOSAL), outcome, zone=ZONE).text == (
        "Not done: I invite only an address you typed."
    )


def test_the_pastes_addresses_are_offered_for_an_allow() -> None:
    outcome = _outcome(pasted=("kim@partner.example", "lee@partner.example"))
    answer = render(_plan(Template.QUOTE), outcome, zone=ZONE)
    assert answer.allow == ("kim@partner.example", "lee@partner.example")
    assert answer.text.endswith(": kim@partner.example, lee@partner.example")


def test_an_empty_answer_says_so() -> None:
    assert render(_plan(Template.EVENTS), _outcome(), zone=ZONE).text == NOTHING_TO_SHOW


# --- plain text ---------------------------------------------------------------------------


def test_the_answer_is_plain_text() -> None:
    """No Markdown and no link: the page shows it as it is (D7)."""
    reading = Reading(
        LABEL,
        (Quoted("See [link: docs.example] and [code removed]", LABEL),),
        (Person(Quoted("**Boss**", LABEL), "boss@example.com", "email"),),
    )
    found = Found((_found(subject="# Heading `code`"),))
    text = "\n".join(
        render(_plan(template), _outcome(result), zone=ZONE).text
        for template, result in ((Template.QUOTE, _read(reading)), (Template.MESSAGES, found))
    )
    assert "http" not in text and "](" not in text
    # Whatever Markdown is here came from mail, inside quotation marks.
    for line in text.splitlines():
        outside = re.sub(f"{OPEN}[^{CLOSE}]*{CLOSE}", "", line)
        assert not re.search(r"[*#`]|\[(?!link|code)", outside), line


# --- from plan to answer, on fakes (the phase's checkpoint) ------------------------------


@pytest.mark.parametrize(
    ("case_id", "expected"),
    [
        (
            "last-from-sara",
            [
                "1 message in your mail:",
                "- from sara.khan@example.com (domain verified by Gmail), Tue 6 Oct 2026 17:00: "
                + _q("Re: Design review agenda"),
            ],
        ),
        (
            "events-tomorrow",
            [
                f"On {CALENDAR}, Thu 8 Oct 2026 00:00 to Fri 9 Oct 2026 00:00:",
                "- Thu 8 Oct 2026 11:00-12:00 Dentist",
                "- Thu 8 Oct 2026 13:00-14:00 Team lunch",
                "- Thu 8 Oct 2026 15:00-16:00, an invitation from ali.raza@example.org: "
                + _q("Budget sync"),
            ],
        ),
        (
            "book-sara-next-week",
            [
                "A card is waiting for your Confirm: Meeting with Sara, Mon 12 Oct 2026 "
                "09:00-09:30, inviting sara.khan@example.com (in your request)."
            ],
        ),
        ("is-ali-free", [DECLINES[Decline.OUT_OF_SCOPE]]),
        ("book-bilal", ["No confirmed contact has that name. Type their email address."]),
    ],
)
def test_a_request_runs_from_its_plan_to_an_answer_on_fakes(
    case_id: str, expected: list[str]
) -> None:
    from app.eval.planner import load_cases, load_world, play
    from app.planner.plan import PlanPayload, to_plan

    world = load_world()
    case = next(case for case in load_cases() if case.id == case_id)
    plan = to_plan(PlanPayload.model_validate(case.ideal), pasted=bool(case.pasted))
    outcome, _ = play(case, world, plan)

    assert render(plan, outcome, zone=world.zone).text.splitlines() == expected


def test_a_quote_loses_what_would_hide_or_turn_it_around() -> None:
    """The renderer's own check, for text that reaches it without `shown`."""
    reading = Reading(LABEL, (Quoted(f"Pay{OVERRIDE} now{BLANK * 50} please", LABEL),), ())
    line = _text(Template.QUOTE, _read(reading)).splitlines()[1]
    assert line == "- " + _q("Pay now please")
