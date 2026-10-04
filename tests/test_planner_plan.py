"""The plan's schema, and the check before any of it runs (M19, D2)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.extraction.payloads import response_json_schema
from app.planner.plan import (
    FIELDS,
    MAX_STEPS,
    Ask,
    Decline,
    FindContact,
    FirstFree,
    FreeBusy,
    InvalidPlanError,
    Kind,
    ListEvents,
    Op,
    Pasted,
    Plan,
    PlanPayload,
    ProposeHold,
    ProposeInvite,
    Read,
    Ref,
    Search,
    Slot,
    StepPayload,
    Template,
    to_plan,
)

ZONE = "Asia/Karachi"
"""Five hours ahead of UTC all year, so each conversion below is plain to check."""

FREEBUSY = {"op": "calendar.freebusy", "start": "2026-10-08T15:00:00", "end": "2026-10-08T16:00:00"}
WEEK = {"op": "calendar.freebusy", "start": "2026-10-12T00:00:00", "end": "2026-10-17T00:00:00"}
SEARCH = {"op": "mail.search", "sender": "sara", "limit": 1}
HOLD = {
    "op": "calendar.propose_hold",
    "title": "Focus",
    "start": "2026-10-08T15:00:00",
    "end": "2026-10-08T16:00:00",
}


def _payload(
    *steps: dict[str, Any],
    kind: str = "answer",
    reply: dict[str, Any] | None = None,
    timezone: str = ZONE,
) -> PlanPayload:
    return PlanPayload.model_validate(
        {
            "kind": kind,
            "steps": list(steps),
            "reply": {"template": "free_busy"} if reply is None else reply,
            "timezone": timezone,
        }
    )


def _plan(*steps: dict[str, Any], pasted: bool = False, **plan: Any) -> Plan:
    return to_plan(_payload(*steps, **plan), pasted=pasted)


def _refused(*steps: dict[str, Any], match: str, pasted: bool = False, **plan: Any) -> str:
    with pytest.raises(InvalidPlanError, match=match) as refused:
        _plan(*steps, pasted=pasted, **plan)
    return str(refused.value)


def _at(hour: int, day: int = 8) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


# --- the schema -------------------------------------------------------------


def _schemas(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _schemas(value)
    elif isinstance(node, list):
        for item in node:
            yield from _schemas(item)


def test_the_schema_has_no_union_no_defs_and_no_additional_properties() -> None:
    schema = response_json_schema(PlanPayload)
    for node in _schemas(schema):
        assert not {"anyOf", "oneOf", "allOf"} & set(node), node
        assert not {"$defs", "$ref", "additionalProperties", "default"} & set(node), node
        assert not isinstance(node.get("type"), list), node


def test_the_schema_requires_the_plan_and_each_steps_operation_only() -> None:
    """Every other step field is optional in the schema and required by code."""
    schema = response_json_schema(PlanPayload)
    step = schema["properties"]["steps"]["items"]
    reply = schema["properties"]["reply"]
    assert set(schema["required"]) == {"kind", "steps", "reply", "timezone"}
    assert step["required"] == ["op"]
    assert reply.get("required", []) == []


def test_the_schema_allows_four_steps() -> None:
    steps = response_json_schema(PlanPayload)["properties"]["steps"]
    assert MAX_STEPS == 4
    assert steps["maxItems"] == MAX_STEPS


def test_the_schema_lists_every_operation() -> None:
    step = response_json_schema(PlanPayload)["properties"]["steps"]["items"]
    assert step["properties"]["op"]["enum"] == [op.value for op in Op]


def test_every_step_field_belongs_to_an_operation() -> None:
    """A field no operation takes would be one the model fills for nothing."""
    taken = set().union(*FIELDS.values())
    assert set(StepPayload.model_fields) - {"op"} == taken
    assert set(FIELDS) == set(Op)


# --- plans that run ---------------------------------------------------------


def test_free_at_a_time() -> None:
    plan = _plan(FREEBUSY)
    assert plan.kind is Kind.ANSWER
    assert plan.template is Template.FREE_BUSY
    assert plan.timezone == ZONE
    assert plan.steps == (FreeBusy(start=_at(10), end=_at(11)),)


def test_a_booking_from_free_busy() -> None:
    plan = _plan(
        WEEK,
        {"op": "slots.first_free", "busy_ref": "$s1", "minutes": 30},
        {
            "op": "calendar.propose_invite",
            "title": "Call with Sara",
            "slot_ref": "$s2",
            "guests": ["Sara@Example.com ", "sara@example.com"],
        },
        reply={"template": "proposal"},
    )
    assert plan.steps[1] == FirstFree(busy=Ref(step=1), minutes=30)
    assert plan.steps[2] == ProposeInvite(
        title="Call with Sara", slot=Ref(step=2), guests=("sara@example.com",), guest_refs=()
    )


def test_a_guest_from_a_confirmed_contact() -> None:
    plan = _plan(
        {"op": "contacts.find", "name": " Sara "},
        {
            "op": "calendar.propose_invite",
            "title": "Lunch",
            "start": "2026-10-08T13:00:00",
            "end": "2026-10-08T14:00:00",
            "guest_refs": ["$s1"],
        },
        reply={"template": "proposal"},
    )
    assert plan.steps[0] == FindContact(name="Sara")
    assert plan.steps[1] == ProposeInvite(
        title="Lunch", slot=Slot(start=_at(8), end=_at(9)), guests=(), guest_refs=(Ref(step=1),)
    )


def test_a_hold() -> None:
    plan = _plan(HOLD, reply={"template": "proposal"})
    assert plan.steps == (ProposeHold(title="Focus", slot=Slot(start=_at(10), end=_at(11))),)


def test_a_search_with_dates_in_the_owners_zone() -> None:
    plan = _plan(
        {**SEARCH, "after": "2026-10-01", "before": "2026-10-03", "limit": 5},
        reply={"template": "messages"},
    )
    midnight_on_the_1st = datetime(2026, 9, 30, 19, tzinfo=UTC)
    midnight_on_the_3rd = datetime(2026, 10, 2, 19, tzinfo=UTC)
    assert plan.steps == (
        Search(
            sender="sara",
            recipient=None,
            words=None,
            after=midnight_on_the_1st,
            before=midnight_on_the_3rd,
            limit=5,
        ),
    )


def test_a_read_of_one_message_from_a_search() -> None:
    plan = _plan(
        SEARCH,
        {"op": "mail.read", "message_ref": "$s1.0", "ask": "asks_of_owner"},
        reply={"template": "quote"},
    )
    assert plan.steps[1] == Read(message=Ref(step=1, item=0), ask=Ask.ASKS_OF_OWNER)


def test_a_read_of_the_pasted_text() -> None:
    plan = _plan(
        {"op": "mail.read", "message_ref": "$paste", "ask": "summary"},
        pasted=True,
        reply={"template": "quote"},
    )
    assert plan.steps == (Read(message=Pasted(), ask=Ask.SUMMARY),)


def test_events_in_a_day() -> None:
    plan = _plan(
        {"op": "calendar.list_events", "start": "2026-10-08", "end": "2026-10-09"},
        reply={"template": "events"},
    )
    assert plan.steps == (ListEvents(start=_at(19, day=7), end=_at(19, day=8)),)


def test_a_question_back() -> None:
    plan = _plan(kind="clarify", reply={"question": "  For how long? "})
    assert (plan.kind, plan.steps, plan.question) == (Kind.CLARIFY, (), "For how long?")


def test_a_refusal() -> None:
    plan = _plan(kind="decline", reply={"decline_code": "cannot_send"})
    assert (plan.kind, plan.steps, plan.decline) == (Kind.DECLINE, (), Decline.CANNOT_SEND)


def test_blank_and_empty_values_count_as_absent() -> None:
    """A model fills an unused field with "" or [] as readily as it leaves it out."""
    plan = _plan(
        {**SEARCH, "words": "  ", "recipient": ""},
        {**HOLD, "guests": [], "slot_ref": ""},
        reply={"template": "proposal"},
    )
    assert plan.steps[0] == Search(
        sender="sara", recipient=None, words=None, after=None, before=None, limit=1
    )


def test_an_offset_the_model_wrote_is_kept() -> None:
    """As in extraction: told not to write one, a model that did meant it."""
    plan = _plan({**FREEBUSY, "start": "2026-10-08T10:00:00+00:00"})
    assert plan.steps == (FreeBusy(start=_at(10), end=_at(11)),)


def test_every_string_is_trimmed_before_use() -> None:
    plan = _plan(FREEBUSY, {"op": "slots.first_free", "busy_ref": " $s1 ", "minutes": 30})
    assert plan.steps[1] == FirstFree(busy=Ref(step=1), minutes=30)


# --- refusals: the schema ---------------------------------------------------


def test_an_unknown_operation_is_refused() -> None:
    with pytest.raises(ValidationError, match="op"):
        _payload({"op": "mail.send", "words": "hello"})


def test_a_fifth_step_is_refused() -> None:
    with pytest.raises(ValidationError, match="steps"):
        _payload(SEARCH, SEARCH, SEARCH, SEARCH, SEARCH, reply={"template": "messages"})


def test_a_field_no_step_has_is_refused() -> None:
    with pytest.raises(ValidationError, match="extra"):
        _payload({**FREEBUSY, "calendar": "primary"})


# --- refusals: missing and wrong fields -------------------------------------


@pytest.mark.parametrize(
    ("step", "field"),
    [
        ({"op": "mail.search"}, "limit"),
        ({"op": "mail.read", "message_ref": "$paste"}, "ask"),
        ({"op": "calendar.freebusy", "start": "2026-10-08T15:00:00"}, "end"),
        ({"op": "calendar.list_events", "end": "2026-10-09"}, "start"),
        ({"op": "contacts.find"}, "name"),
        ({**HOLD, "title": " "}, "title"),
        ({"op": "calendar.propose_hold", "title": "Focus"}, "start"),
        ({**HOLD, "end": None}, "end"),
    ],
)
def test_a_missing_field_is_refused(step: dict[str, Any], field: str) -> None:
    _refused(step, pasted=True, match=f"step 1: {field} is missing")


def test_a_missing_minutes_is_refused() -> None:
    _refused(FREEBUSY, {"op": "slots.first_free", "busy_ref": "$s1"}, match="minutes is missing")


def test_an_invite_without_a_guest_is_refused() -> None:
    invite = {**HOLD, "op": "calendar.propose_invite"}
    _refused(invite, reply={"template": "proposal"}, match="step 1: guests is missing")


@pytest.mark.parametrize(
    ("step", "field"),
    [
        ({**HOLD, "guests": ["sara@example.com"]}, "guests"),
        ({**SEARCH, "title": "Sara"}, "title"),
        ({**FREEBUSY, "busy_ref": "$s1"}, "busy_ref"),
        ({**SEARCH, "start": "2026-10-08T15:00:00"}, "start"),
    ],
)
def test_a_field_the_operation_does_not_take_is_refused(step: dict[str, Any], field: str) -> None:
    """A hold given guests is an invite the planner mislabelled: dropping them quietly
    would book the owner alone."""
    _refused(step, match=f"step 1: {field} is not taken by {step['op']}")


def test_a_slot_given_two_ways_is_refused() -> None:
    _refused(
        FREEBUSY,
        {"op": "slots.first_free", "busy_ref": "$s1", "minutes": 30},
        {**HOLD, "slot_ref": "$s2"},
        reply={"template": "proposal"},
        match="step 3: slot_ref is given with start or end",
    )


# --- refusals: references ---------------------------------------------------


def test_a_list_where_one_message_is_wanted_is_refused() -> None:
    _refused(
        SEARCH,
        {"op": "mail.read", "message_ref": "$s1", "ask": "summary"},
        reply={"template": "quote"},
        match="step 2: message_ref names a list where one value is wanted",
    )


def test_an_item_of_a_result_that_is_not_a_list_is_refused() -> None:
    _refused(
        FREEBUSY,
        {"op": "slots.first_free", "busy_ref": "$s1.0", "minutes": 30},
        match="step 2: busy_ref names an item of a result that is not a list",
    )


@pytest.mark.parametrize(
    ("steps", "match"),
    [
        (
            [FREEBUSY, {"op": "calendar.propose_hold", "title": "Focus", "slot_ref": "$s1"}],
            "step 2: slot_ref names a calendar.freebusy step, not slots.first_free",
        ),
        (
            [SEARCH, {**HOLD, "op": "calendar.propose_invite", "guest_refs": ["$s1"]}],
            "step 2: guest_refs names a mail.search step, not contacts.find",
        ),
        (
            [
                {"op": "calendar.list_events", "start": "2026-10-08", "end": "2026-10-09"},
                {"op": "mail.read", "message_ref": "$s1.0", "ask": "summary"},
            ],
            "step 2: message_ref names a calendar.list_events step, not mail.search",
        ),
    ],
)
def test_a_reference_to_the_wrong_kind_of_result_is_refused(
    steps: list[dict[str, Any]], match: str
) -> None:
    _refused(*steps, reply={"template": "proposal"}, match=match)


def test_a_forward_reference_is_refused() -> None:
    _refused(
        {"op": "mail.read", "message_ref": "$s2.0", "ask": "summary"},
        SEARCH,
        reply={"template": "quote"},
        match="step 1: message_ref points forward",
    )


def test_a_step_that_names_itself_is_refused() -> None:
    _refused(
        FREEBUSY,
        {"op": "slots.first_free", "busy_ref": "$s2", "minutes": 30},
        match="step 2: busy_ref points forward",
    )


@pytest.mark.parametrize("ref", ["$s5", "$s0", "$s3"])
def test_a_reference_to_no_step_is_refused(ref: str) -> None:
    _refused(
        FREEBUSY,
        {"op": "slots.first_free", "busy_ref": ref, "minutes": 30},
        match="step 2: busy_ref points to no step",
    )


def test_an_item_past_what_the_search_returns_is_refused() -> None:
    _refused(
        {**SEARCH, "limit": 2},
        {"op": "mail.read", "message_ref": "$s1.2", "ask": "summary"},
        reply={"template": "quote"},
        match="step 2: message_ref points past what step 1 returns",
    )


def test_the_paste_with_nothing_pasted_is_refused() -> None:
    _refused(
        {"op": "mail.read", "message_ref": "$paste", "ask": "summary"},
        reply={"template": "quote"},
        match="step 1: message_ref points to no pasted text",
    )


@pytest.mark.parametrize("ref", ["s1", "$s1.x", "$step1", "$s1.0.1", "$S1", "$s 1"])
def test_a_malformed_reference_is_refused(ref: str) -> None:
    _refused(
        FREEBUSY,
        {"op": "slots.first_free", "busy_ref": ref, "minutes": 30},
        match="step 2: busy_ref is not a reference",
    )


# --- refusals: the plan as a whole ------------------------------------------


@pytest.mark.parametrize(
    ("kind", "steps", "reply", "match"),
    [
        ("answer", [], {"template": "free_busy"}, "an answer plan runs at least one step"),
        ("clarify", [FREEBUSY], {"question": "When?"}, "a clarify plan runs no steps"),
        ("decline", [SEARCH], {"decline_code": "cannot_send"}, "a decline plan runs no steps"),
        ("answer", [FREEBUSY], {}, "reply.template is missing"),
        ("clarify", [], {"question": " "}, "reply.question is missing"),
        ("decline", [], {}, "reply.decline_code is missing"),
        (
            "answer",
            [FREEBUSY],
            {"template": "free_busy", "question": "When?"},
            "reply.question is not taken by an answer plan",
        ),
        (
            "clarify",
            [],
            {"question": "When?", "template": "events"},
            "reply.template is not taken by a clarify plan",
        ),
        (
            "decline",
            [],
            {"decline_code": "cannot_send", "question": "Why?"},
            "reply.question is not taken by a decline plan",
        ),
    ],
)
def test_a_reply_that_does_not_fit_its_kind_is_refused(
    kind: str, steps: list[dict[str, Any]], reply: dict[str, Any], match: str
) -> None:
    _refused(*steps, kind=kind, reply=reply, match=match)


@pytest.mark.parametrize(
    ("steps", "template", "match"),
    [
        ([FREEBUSY], "events", "reply.template events has no calendar.list_events step"),
        ([FREEBUSY], "proposal", "reply.template proposal has no calendar.propose_hold"),
        ([HOLD], "free_busy", "a proposal is answered with reply.template proposal"),
    ],
)
def test_a_template_with_nothing_to_show_is_refused(
    steps: list[dict[str, Any]], template: str, match: str
) -> None:
    _refused(*steps, reply={"template": template}, match=match)


def test_a_fourth_read_is_refused() -> None:
    read = {"op": "mail.read", "message_ref": "$paste", "ask": "summary"}
    _refused(
        read, read, read, read, pasted=True, reply={"template": "quote"}, match="at most 3 reads"
    )


@pytest.mark.parametrize("zone", ["PKT", "+05:00", "Asia/Nowhere", ""])
def test_a_zone_that_is_not_one_is_refused(zone: str) -> None:
    _refused(FREEBUSY, timezone=zone, match="timezone is not a known zone")


# --- refusals: values -------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "match"),
    [
        ({**FREEBUSY, "start": "tomorrow at three"}, "step 1: start is not a local time"),
        ({**FREEBUSY, "end": "2026-10-08T15:00:00"}, "step 1: end is not after start"),
        ({**FREEBUSY, "end": "2026-11-09T15:00:00"}, "step 1: the range is longer than 31 days"),
        ({**SEARCH, "after": "last week"}, "step 1: after is not a date"),
        ({**SEARCH, "after": "2026-10-03", "before": "2026-10-03"}, "after is not earlier than"),
        ({**SEARCH, "limit": 0}, "step 1: limit is not between 1 and 10"),
        ({**SEARCH, "limit": 11}, "step 1: limit is not between 1 and 10"),
        ({**SEARCH, "words": "w" * 201}, "step 1: words is longer than 200 characters"),
    ],
)
def test_a_value_that_cannot_be_used_is_refused(step: dict[str, Any], match: str) -> None:
    _refused(step, reply={"template": "messages"}, match=match)


def test_a_slot_longer_than_a_day_is_refused() -> None:
    hold = {**HOLD, "end": "2026-10-09T16:00:00"}
    _refused(hold, reply={"template": "proposal"}, match="step 1: the slot is longer than 24 hours")


@pytest.mark.parametrize("minutes", [0, 24 * 60 + 1])
def test_minutes_out_of_range_are_refused(minutes: int) -> None:
    _refused(
        FREEBUSY,
        {"op": "slots.first_free", "busy_ref": "$s1", "minutes": minutes},
        match="step 2: minutes is not between 1 and 1440",
    )


def test_a_guest_that_is_not_an_address_is_refused() -> None:
    invite = {**HOLD, "op": "calendar.propose_invite", "guests": ["sara at example dot com"]}
    _refused(invite, reply={"template": "proposal"}, match="step 1: guests holds a non-address")


def test_a_refusal_never_quotes_the_value() -> None:
    """Stored errors name the field and the problem, not what the model wrote (M18, D7)."""
    invite = {**HOLD, "op": "calendar.propose_invite", "guests": ["quoted-value-marker"]}
    message = _refused(invite, reply={"template": "proposal"}, match="non-address")
    assert "quoted-value-marker" not in message
    message = _refused({**HOLD, "title": "quoted-title-marker " * 20}, match="longer than")
    assert "quoted-title-marker" not in message
