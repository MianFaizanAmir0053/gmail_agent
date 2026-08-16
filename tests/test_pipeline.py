"""Pipeline behaviour against a fake Gemini client. No network, no API key."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.contracts import EmailMessage
from app.eval.dataset import load_fixtures
from app.extraction.llm import BlockedError, LlmError, TruncatedError, structured_call
from app.extraction.payloads import ClassifyPayload, ExtractionPayload, response_json_schema
from app.extraction.pipeline import ExtractionPipeline
from app.extraction.prompts import CLASSIFY_SYSTEM, EXTRACT_SYSTEM, user_content

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

CLASSIFY_YES = {"is_meeting": True, "confidence": 0.9, "reasoning": "Explicit time."}
CLASSIFY_NO = {"is_meeting": False, "confidence": 0.95, "reasoning": "Job alert."}
EXTRACTION = {
    "is_meeting": True,
    "title": "Design review",
    "start_local": "2026-08-19T16:00:00",
    "end_local": "2026-08-19T17:00:00",
    "timezone": "Asia/Karachi",
    "attendees": ["sara@example.com"],
    "location": None,
    "confidence": 0.9,
    "reasoning": "Wednesday 19 August, 4pm PKT.",
}


class _Candidate:
    def __init__(self, finish_reason: str) -> None:
        self.finish_reason = finish_reason


class _Feedback:
    def __init__(self, block_reason: str | None) -> None:
        self.block_reason = block_reason


class _UsageMetadata:
    def __init__(self, cached: int) -> None:
        self.prompt_token_count = 1200
        self.candidates_token_count = 90
        self.cached_content_token_count = cached
        self.thoughts_token_count = 40


class _Response:
    def __init__(
        self,
        payload: dict[str, Any] | None,
        *,
        finish_reason: str = "STOP",
        block_reason: str | None = None,
        cached: int = 0,
        candidates: bool = True,
    ) -> None:
        self.text = json.dumps(payload) if payload is not None else None
        self.candidates = [_Candidate(finish_reason)] if candidates else []
        self.prompt_feedback = _Feedback(block_reason)
        self.usage_metadata = _UsageMetadata(cached)


@dataclass
class FakeModels:
    responses: list[_Response]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate_content(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("Pipeline made more calls than the fake was primed for")
        return self.responses.pop(0)


@dataclass
class FakeClient:
    models: FakeModels


def _client(*responses: _Response) -> FakeClient:
    return FakeClient(models=FakeModels(list(responses)))


def _pipeline(client: FakeClient) -> ExtractionPipeline:
    return ExtractionPipeline(
        client=client,
        classify_model="gemini-2.5-flash",
        extraction_model="gemini-2.5-pro",
        owner_email="me@example.com",
    )


def _email(subject: str = "Design review", body: str = "Wednesday 4pm") -> EmailMessage:
    return EmailMessage(
        id="m1",
        thread_id="m1",
        subject=subject,
        body_text=body,
        sender="sara@example.com",
        recipients=["me@example.com"],
        received_at=NOW,
    )


def _call(client: FakeClient, schema: type[ClassifyPayload] = ClassifyPayload) -> Any:
    return structured_call(
        client, model="gemini-2.5-flash", system=CLASSIFY_SYSTEM, user="x", schema=schema
    )


# --- two-stage behaviour ---------------------------------------------------


def test_non_meeting_skips_the_expensive_call() -> None:
    """The whole point of triage: newsletters must not reach extraction."""
    client = _client(_Response(CLASSIFY_NO))
    pipeline = _pipeline(client)

    result = pipeline(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert result.is_meeting is False
    assert result.reasoning == "Job alert."
    assert pipeline.stats.classify_calls == 1
    assert pipeline.stats.extract_calls == 0
    assert len(client.models.calls) == 1


def test_meeting_runs_both_stages_and_converts_to_utc() -> None:
    client = _client(_Response(CLASSIFY_YES), _Response(EXTRACTION))
    pipeline = _pipeline(client)

    result = pipeline(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert result.is_meeting is True
    assert result.start_utc == datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
    assert result.timezone == "Asia/Karachi"
    assert pipeline.stats.extract_calls == 1


def test_the_two_stages_use_different_models() -> None:
    """Triage is a cheap yes/no; extraction is not. Same model for both wastes money."""
    client = _client(_Response(CLASSIFY_YES), _Response(EXTRACTION))
    _pipeline(client)(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert client.models.calls[0]["model"] == "gemini-2.5-flash"
    assert client.models.calls[1]["model"] == "gemini-2.5-pro"


def test_owner_removed_from_attendees_end_to_end() -> None:
    payload = EXTRACTION | {"attendees": ["sara@example.com", "me@example.com"]}
    pipeline = _pipeline(_client(_Response(CLASSIFY_YES), _Response(payload)))

    result = pipeline(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert result.attendees == ["sara@example.com"]


def test_malformed_extraction_degrades_instead_of_raising() -> None:
    """A null-start "meeting" would blow up later, further from the cause."""
    payload = EXTRACTION | {"timezone": "Mars/Olympus"}
    pipeline = _pipeline(_client(_Response(CLASSIFY_YES), _Response(payload)))

    result = pipeline(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert result.is_meeting is False
    assert "malformed" in result.reasoning.lower()


# --- prompt construction ---------------------------------------------------


def test_each_stage_gets_its_own_system_instruction() -> None:
    client = _client(_Response(CLASSIFY_YES), _Response(EXTRACTION))
    _pipeline(client)(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert client.models.calls[0]["config"].system_instruction == CLASSIFY_SYSTEM
    assert client.models.calls[1]["config"].system_instruction == EXTRACT_SYSTEM


def test_current_time_never_reaches_the_system_instruction() -> None:
    """Volatile content in the stable prefix defeats implicit caching.

    Static dates inside the worked examples are fine -- they never change. What
    must not appear is the grounding instant.
    """
    for text in (CLASSIFY_SYSTEM, EXTRACT_SYSTEM):
        assert "2026-08-17" not in text


def test_grounding_lives_in_the_user_turn_and_varies_with_now() -> None:
    content = user_content(_email(), now_utc=NOW, user_timezone="Asia/Karachi")
    assert "2026-08-17" in content
    assert "Monday" in content  # weekday spelled out for relative-date resolution

    later = user_content(_email(), now_utc=NOW + timedelta(days=30), user_timezone="Asia/Karachi")
    assert later != content


def test_examples_do_not_leak_golden_answers() -> None:
    """Teaching conventions is fine; teaching the eval's answers is not."""
    for fixture in load_fixtures():
        if fixture.expected.title:
            assert fixture.expected.title not in EXTRACT_SYSTEM
        assert fixture.email.subject not in EXTRACT_SYSTEM


def test_triage_thinks_minimally_and_extraction_uses_the_default() -> None:
    """Reasoning tokens on "is this a job alert" are the easiest saving here."""
    client = _client(_Response(CLASSIFY_YES), _Response(EXTRACTION))
    _pipeline(client)(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert client.models.calls[0]["config"].thinking_config.thinking_level == "MINIMAL"
    assert client.models.calls[1]["config"].thinking_config is None


def test_thinking_budget_is_never_sent() -> None:
    """The 2.x integer knob 400s on 3.x models with an error that names nothing."""
    client = _client(_Response(CLASSIFY_YES), _Response(EXTRACTION))
    _pipeline(client)(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert client.models.calls[0]["config"].thinking_config.thinking_budget is None


def test_json_output_is_requested_with_a_cleaned_schema() -> None:
    """The schema goes as a scrubbed dict, not the Pydantic class.

    Passing the class makes the SDK forward `additionalProperties` (from
    `extra="forbid"`), which the endpoint rejects with a 400.
    """
    client = _client(_Response(CLASSIFY_YES), _Response(EXTRACTION))
    _pipeline(client)(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    config = client.models.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == response_json_schema(ClassifyPayload)
    assert "additionalProperties" not in str(config.response_json_schema)


def test_cleaned_schema_keeps_fields_whose_name_matches_a_schema_keyword() -> None:
    """`title` is both a JSON Schema keyword and a real payload field.

    Stripping by key name once deleted `properties.title` while leaving it in
    `required`, producing a 400 that blamed the top-level schema.
    """
    schema = response_json_schema(ExtractionPayload)
    assert "title" in schema["properties"]
    assert "title" in schema["required"]


def test_cache_hits_are_counted() -> None:
    client = _client(_Response(CLASSIFY_YES, cached=900), _Response(EXTRACTION))
    pipeline = _pipeline(client)
    pipeline(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert pipeline.stats.cache_hits == 1


def test_thinking_tokens_are_tracked_separately() -> None:
    """They bill differently from output; folding them together hides the cost."""
    client = _client(_Response(CLASSIFY_YES), _Response(EXTRACTION))
    pipeline = _pipeline(client)
    pipeline(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert pipeline.stats.thinking_tokens == 80
    assert pipeline.stats.output_tokens == 180


# --- failure modes ---------------------------------------------------------


def test_blocked_prompt_is_detected_before_reading_text() -> None:
    """A blocked response has no usable text; reading it first buries the cause."""
    with pytest.raises(BlockedError, match="Prompt blocked"):
        _call(_client(_Response(None, block_reason="SAFETY")))


def test_blocked_response_is_detected_from_the_finish_reason() -> None:
    with pytest.raises(BlockedError, match="finish_reason=PROHIBITED_CONTENT"):
        _call(_client(_Response(None, finish_reason="PROHIBITED_CONTENT")))


def test_recitation_counts_as_blocked() -> None:
    with pytest.raises(BlockedError):
        _call(_client(_Response(None, finish_reason="RECITATION")))


def test_truncation_is_reported_as_such_not_as_a_schema_error() -> None:
    """Truncation is fixable with a bigger budget; a schema error is not."""
    with pytest.raises(TruncatedError):
        _call(_client(_Response({"is_meeting": True}, finish_reason="MAX_TOKENS")))


def test_empty_candidate_list_is_an_error() -> None:
    with pytest.raises(LlmError, match="no candidates"):
        _call(_client(_Response(None, candidates=False)))


def test_schema_violation_names_the_model() -> None:
    with pytest.raises(LlmError, match="ClassifyPayload"):
        _call(_client(_Response({"is_meeting": "yes please"})))


def test_unspecified_block_reason_is_not_treated_as_a_block() -> None:
    """The enum's zero value means "nothing to report", not "blocked"."""
    completion = _call(_client(_Response(CLASSIFY_YES, block_reason="BLOCKED_REASON_UNSPECIFIED")))
    assert completion.parsed.is_meeting is True
