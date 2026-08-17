"""The reviewer agent and the loop that contains it. No network, no API key."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.reviewer import Corrections, Reviewer, ReviewVerdict
from app.contracts import EmailMessage, ExtractionResult
from app.eval.reviewed import ReviewedExtractor
from app.google.calendar import BusyInterval
from app.rag.search import Hit
from app.tools.calendar_tool import FREEBUSY_TOOL, execute_freebusy

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

APPROVE = {
    "decision": "approve",
    "issues": [],
    "corrections": {},
    "confidence": 0.9,
    "reasoning": "Checked the slot and the zone; both correct.",
}
REVISE = {
    "decision": "revise",
    "issues": ["The sender said 9am Pacific; the result used the recipient's zone."],
    "corrections": {"timezone": "America/Los_Angeles"},
    "confidence": 0.8,
    "reasoning": "Stated zone was ignored.",
}
REJECT = {
    "decision": "reject",
    "issues": ["This cancels an existing meeting; it does not create one."],
    "corrections": {},
    "confidence": 0.95,
    "reasoning": "Cancellation.",
}


class _FunctionCall:
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.name = name
        self.args = args


class _Part:
    def __init__(self, function_call: _FunctionCall | None = None) -> None:
        self.function_call = function_call


class _Content:
    def __init__(self, parts: list[_Part]) -> None:
        self.parts = parts
        self.role = "model"


class _Candidate:
    def __init__(self, parts: list[_Part]) -> None:
        self.finish_reason = "STOP"
        self.content = _Content(parts)


class _Usage:
    def __init__(self) -> None:
        self.prompt_token_count = 500
        self.candidates_token_count = 60
        self.cached_content_token_count = 0
        self.thoughts_token_count = 10


class _Response:
    def __init__(
        self, payload: dict[str, Any] | None = None, calls: list[_FunctionCall] | None = None
    ) -> None:
        self.text = json.dumps(payload) if payload is not None else None
        self.candidates = [_Candidate([_Part(c) for c in (calls or [])])]
        self.prompt_feedback = None
        self.usage_metadata = _Usage()


@dataclass
class FakeModels:
    responses: list[_Response]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate_content(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("more calls than the fake was primed for")
        return self.responses.pop(0)


@dataclass
class FakeClient:
    models: FakeModels


def _client(*responses: _Response) -> FakeClient:
    return FakeClient(models=FakeModels(list(responses)))


class FakeCalendar:
    def __init__(self, busy: list[BusyInterval] | None = None) -> None:
        self.busy = busy or []
        self.queries: list[tuple[datetime, datetime]] = []

    def freebusy(self, start: datetime, end: datetime) -> list[BusyInterval]:
        self.queries.append((start, end))
        return self.busy


def _email(body: str = "Let's meet Thursday at 3pm.") -> EmailMessage:
    return EmailMessage(
        id="m1",
        thread_id="m1",
        subject="Design review",
        body_text=body,
        sender="sara@example.com",
        recipients=["me@example.com"],
        received_at=NOW,
    )


def _extraction(**overrides: Any) -> ExtractionResult:
    base: dict[str, Any] = {
        "is_meeting": True,
        "title": "Design review",
        "start_utc": datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 20, 11, 0, tzinfo=UTC),
        "timezone": "Asia/Karachi",
        "attendees": ["sara@example.com"],
        "confidence": 0.9,
        "reasoning": "Thursday 3pm local.",
    }
    base.update(overrides)
    return ExtractionResult(**base)


# --- tools ------------------------------------------------------------------


def test_a_reviewer_with_no_connections_declares_no_tools() -> None:
    """A declared tool with nothing behind it costs a turn and returns an error."""
    assert Reviewer(client=_client(), model="m").tools == []


def test_tools_appear_only_when_wired() -> None:
    with_calendar = Reviewer(client=_client(), model="m", calendar=FakeCalendar())  # type: ignore[arg-type]
    names = [tool["name"] for tool in with_calendar.tools]
    assert names == ["freebusy_check"]


def test_freebusy_reports_a_free_slot_in_words() -> None:
    result = execute_freebusy(
        FakeCalendar(),  # type: ignore[arg-type]
        {"start_utc": "2026-08-20T10:00:00Z", "end_utc": "2026-08-20T11:00:00Z"},
    )
    assert result["busy"] == []
    assert "free" in result["note"]


def test_freebusy_reports_a_clash() -> None:
    busy = [
        BusyInterval(
            start=datetime(2026, 8, 20, 10, 30, tzinfo=UTC),
            end=datetime(2026, 8, 20, 11, 30, tzinfo=UTC),
        )
    ]
    result = execute_freebusy(
        FakeCalendar(busy),  # type: ignore[arg-type]
        {"start_utc": "2026-08-20T10:00:00Z", "end_utc": "2026-08-20T11:00:00Z"},
    )
    assert len(result["busy"]) == 1


def test_bad_freebusy_arguments_are_answered_not_raised() -> None:
    result = execute_freebusy(FakeCalendar(), {"start_utc": "whenever"})  # type: ignore[arg-type]
    assert "error" in result


def test_a_calendar_outage_costs_one_check_not_the_extraction() -> None:
    class Broken:
        def freebusy(self, start: datetime, end: datetime) -> list[BusyInterval]:
            raise RuntimeError("calendar unavailable")

    result = execute_freebusy(
        Broken(),  # type: ignore[arg-type]
        {"start_utc": "2026-08-20T10:00:00Z", "end_utc": "2026-08-20T11:00:00Z"},
    )
    assert "calendar unavailable" in result["error"]


def test_the_freebusy_declaration_avoids_what_gemini_rejects() -> None:
    rendered = repr(FREEBUSY_TOOL)
    assert "additionalProperties" not in rendered
    assert "anyOf" not in rendered


# --- verdicts ---------------------------------------------------------------


def test_an_approval_comes_back_clean() -> None:
    reviewer = Reviewer(client=_client(_Response(APPROVE)), model="m")
    verdict = reviewer(_email(), _extraction(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert verdict.decision == "approve"
    assert reviewer.stats.approvals == 1


def test_the_reviewer_actually_calls_its_tools() -> None:
    """The difference between an agent and a second opinion."""
    calendar = FakeCalendar()
    reviewer = Reviewer(
        client=_client(
            _Response(
                calls=[
                    _FunctionCall(
                        "freebusy_check",
                        {"start_utc": "2026-08-20T10:00:00Z", "end_utc": "2026-08-20T11:00:00Z"},
                    )
                ]
            ),
            _Response(APPROVE),
        ),
        model="m",
        calendar=calendar,  # type: ignore[arg-type]
    )

    reviewer(_email(), _extraction(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert calendar.queries
    assert reviewer.stats.tool_calls == 1


def test_feedback_carries_issues_and_suggestions() -> None:
    verdict = ReviewVerdict.model_validate(REVISE)
    feedback = verdict.feedback()
    assert "9am Pacific" in feedback
    assert "America/Los_Angeles" in feedback


def test_corrections_omit_what_was_not_corrected() -> None:
    assert Corrections(timezone="Europe/Berlin").describe() == "timezone='Europe/Berlin'"


# --- the loop ---------------------------------------------------------------


@dataclass
class FakePipeline:
    extractions: list[ExtractionResult]
    is_meeting: bool = True
    extras: list[str] = field(default_factory=list)

    def classify(self, email: EmailMessage, **kwargs: Any) -> Any:
        from app.extraction.payloads import ClassifyPayload

        return ClassifyPayload(is_meeting=self.is_meeting, confidence=0.9, reasoning="ok")

    def extract(self, email: EmailMessage, *, extra: str = "", **kwargs: Any) -> ExtractionResult:
        self.extras.append(extra)
        return self.extractions.pop(0)


def _reviewed(pipeline: Any, *responses: _Response) -> ReviewedExtractor:
    return ReviewedExtractor(
        pipeline=pipeline, reviewer=Reviewer(client=_client(*responses), model="m")
    )


def test_an_approved_extraction_is_returned_untouched() -> None:
    pipeline = FakePipeline([_extraction()])
    result = _reviewed(pipeline, _Response(APPROVE))(
        _email(), now_utc=NOW, user_timezone="Asia/Karachi"
    )
    assert result.title == "Design review"
    assert pipeline.extras == [""]


def test_a_revision_re_extracts_with_the_feedback() -> None:
    pipeline = FakePipeline([_extraction(), _extraction(title="Design review (PT)")])
    result = _reviewed(pipeline, _Response(REVISE), _Response(APPROVE))(
        _email(), now_utc=NOW, user_timezone="Asia/Karachi"
    )

    assert result.title == "Design review (PT)"
    assert "9am Pacific" in pipeline.extras[1]
    assert "reviewer" in pipeline.extras[1].lower()


def test_a_rejection_becomes_not_a_meeting() -> None:
    pipeline = FakePipeline([_extraction()])
    result = _reviewed(pipeline, _Response(REJECT))(
        _email(), now_utc=NOW, user_timezone="Asia/Karachi"
    )

    assert result.is_meeting is False
    assert "cancels an existing meeting" in result.reasoning


def test_the_loop_is_bounded_however_stubborn_the_reviewer() -> None:
    """A cap in the prompt is a request; this one is arithmetic."""
    pipeline = FakePipeline([_extraction() for _ in range(6)])
    reviewed = _reviewed(pipeline, *[_Response(REVISE) for _ in range(6)])

    result = reviewed(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert result.is_meeting is True
    assert reviewed.reviewer.stats.reviews == 3


def test_a_non_meeting_never_reaches_the_reviewer() -> None:
    """Paying a call to confirm a newsletter is still not a meeting."""
    pipeline = FakePipeline([], is_meeting=False)
    reviewed = _reviewed(pipeline)

    result = reviewed(_email("Your invoice is attached."), now_utc=NOW, user_timezone="UTC")

    assert result.is_meeting is False
    assert reviewed.reviewer.stats.reviews == 0


# --- retrieval seam ---------------------------------------------------------


def test_the_reviewer_can_search_when_a_searcher_is_wired() -> None:
    def searcher(query: str, **kwargs: Any) -> list[Hit]:
        return [
            Hit(
                chunk_id=1,
                thread_id="t",
                message_id="m9",
                subject="Re: design",
                content="Sara Iqbal, always on this thread.",
                participants=("sara.iqbal@example.com",),
                sent_at=NOW,
                score=0.03,
            )
        ]

    reviewer = Reviewer(
        client=_client(
            _Response(calls=[_FunctionCall("search_context", {"query": "Sara"})]),
            _Response(APPROVE),
        ),
        model="m",
        searcher=searcher,
    )

    reviewer(_email(), _extraction(), now_utc=NOW, user_timezone="Asia/Karachi")
    assert reviewer.stats.tool_calls == 1


def test_a_tool_the_reviewer_does_not_have_is_answered_not_raised() -> None:
    reviewer = Reviewer(
        client=_client(
            _Response(calls=[_FunctionCall("freebusy_check", {})]),
            _Response(APPROVE),
        ),
        model="m",
        searcher=lambda *a, **k: [],
    )

    verdict = reviewer(_email(), _extraction(), now_utc=NOW, user_timezone="Asia/Karachi")
    assert verdict.decision == "approve"


def test_the_verdict_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReviewVerdict.model_validate({**APPROVE, "verdict": "lgtm"})
