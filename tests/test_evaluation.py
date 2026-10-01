"""Triage on an evaluation model through Vercel AI Gateway. No network, no key."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.contracts import EmailMessage
from app.eval.dataset import load_fixtures
from app.extraction import prompts
from app.extraction.evaluation import (
    JEV,
    STATE_CHAR_LIMIT,
    GatewayError,
    classify_by_evaluation,
)
from app.extraction.llm import LlmError
from app.extraction.pipeline import ExtractionPipeline
from app.policy.models import EVALUATE_URL, GatewayEvaluator

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)

EXTRACTION = {
    "is_meeting": True,
    "title": "Design review",
    "start_local": "2026-10-01T16:00:00",
    "end_local": "2026-10-01T17:00:00",
    "timezone": "Asia/Karachi",
    "attendees": ["sara@example.com"],
    "location": None,
    "confidence": 0.9,
    "reasoning": "Thursday 1 October, 4pm PKT.",
}


def _answer(probability: float, *, input_tokens: int = 275) -> dict[str, Any]:
    return {
        "model": JEV,
        "answers": {"calendar_event": {"type": "boolean", "probability": probability}},
        "usage": {"inputTokens": input_tokens, "outputTokens": 20},
    }


@dataclass
class FakeEvaluator:
    replies: list[dict[str, Any] | Exception]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _classify(evaluator: FakeEvaluator, state: str = "Design review Thursday 4pm?") -> Any:
    return classify_by_evaluation(evaluator, model=JEV, state=state, base_delay=0.0)


# --- the question ----------------------------------------------------------


def test_triage_is_one_boolean_question_about_the_email() -> None:
    evaluator = FakeEvaluator([_answer(0.9)])
    _classify(evaluator, state="the email")

    (request,) = evaluator.requests
    assert request["model"] == JEV
    assert request["state"] == "the email"
    (question,) = request["questions"].values()
    assert question["type"] == "boolean"
    assert question["instructions"] == prompts.MEETING_QUESTION
    assert set(question["criteria"]) == {"true", "false"}


def test_the_question_does_not_leak_golden_answers() -> None:
    """Teaching conventions is fine; teaching the eval's answers is not."""
    text = prompts.MEETING_QUESTION + json.dumps(prompts.MEETING_CRITERIA)
    for fixture in load_fixtures():
        assert fixture.email.subject not in text
        if fixture.expected.title:
            assert fixture.expected.title not in text


# --- the verdict -----------------------------------------------------------


def test_a_likely_meeting_passes_triage_with_its_probability_as_confidence() -> None:
    verdict, _ = _classify(FakeEvaluator([_answer(0.93)]))
    assert verdict.is_meeting is True
    assert verdict.confidence == pytest.approx(0.93)
    assert "0.93" in verdict.reasoning


def test_an_unlikely_meeting_is_rejected_with_the_complement_as_confidence() -> None:
    """Confidence is in the verdict, so a 0.1 chance of a meeting is a 0.9
    confident no -- not a 0.1 confident one."""
    verdict, _ = _classify(FakeEvaluator([_answer(0.1)]))
    assert verdict.is_meeting is False
    assert verdict.confidence == pytest.approx(0.9)


def test_a_coin_flip_goes_on_to_extraction() -> None:
    """A triage miss loses a meeting silently; a triage false positive only
    costs an extraction call that can still say no."""
    verdict, _ = _classify(FakeEvaluator([_answer(0.5)]))
    assert verdict.is_meeting is True


@pytest.mark.parametrize("probability", [-0.1, 1.5, None, "0.9", True])
def test_an_unusable_probability_is_an_error(probability: object) -> None:
    body = _answer(0.9)
    body["answers"]["calendar_event"]["probability"] = probability
    with pytest.raises(LlmError, match="probability"):
        _classify(FakeEvaluator([body]))


def test_a_missing_answer_is_an_error() -> None:
    with pytest.raises(LlmError, match="probability"):
        _classify(FakeEvaluator([{"model": JEV, "answers": {}, "usage": {}}]))


# --- usage -----------------------------------------------------------------


def test_usage_is_reported_to_the_enclosing_span(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "app.extraction.evaluation.record_llm_usage", lambda **kw: recorded.append(kw)
    )

    _, usage = _classify(FakeEvaluator([_answer(0.2, input_tokens=275)]))

    assert (usage.input_tokens, usage.output_tokens) == (275, 20)
    assert recorded == [
        {
            "model": JEV,
            "input_tokens": 275,
            "output_tokens": 20,
            "cached_tokens": 0,
            "thinking_tokens": 0,
        }
    ]


def test_usage_is_recorded_even_when_the_answer_is_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tokens were billed either way. Dropping them would make a bad
    answer look free."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "app.extraction.evaluation.record_llm_usage", lambda **kw: recorded.append(kw)
    )
    body = {"model": JEV, "answers": {}, "usage": {"inputTokens": 300, "outputTokens": 0}}

    with pytest.raises(LlmError):
        _classify(FakeEvaluator([body]))

    assert [r["input_tokens"] for r in recorded] == [300]


# --- failure handling ------------------------------------------------------


def test_a_transient_gateway_error_is_retried() -> None:
    evaluator = FakeEvaluator([GatewayError(503, "busy"), _answer(0.8)])
    verdict, _ = _classify(evaluator)
    assert verdict.is_meeting is True
    assert len(evaluator.requests) == 2


def test_a_rejected_request_is_not_retried() -> None:
    """A 400 never improves on retry; resending it only burns time."""
    evaluator = FakeEvaluator([GatewayError(400, "questions: invalid"), _answer(0.8)])
    with pytest.raises(GatewayError, match="400"):
        _classify(evaluator)
    assert len(evaluator.requests) == 1


def test_an_oversized_email_is_cut_and_the_verdict_says_so() -> None:
    """Jev reads at most 32k tokens of state. Cutting quietly would make a
    miss on a long thread impossible to explain later."""
    evaluator = FakeEvaluator([_answer(0.2)])
    verdict, _ = _classify(evaluator, state="x" * (STATE_CHAR_LIMIT + 500))

    assert len(evaluator.requests[0]["state"]) == STATE_CHAR_LIMIT
    assert "cut" in verdict.reasoning


# --- the HTTP layer --------------------------------------------------------


def _evaluator(handler: Any) -> GatewayEvaluator:
    return GatewayEvaluator(api_key="test-key", transport=httpx.MockTransport(handler))


def test_the_request_is_an_authenticated_json_post_to_the_evaluate_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_answer(0.7))

    body = _evaluator(handler).evaluate({"model": JEV, "state": "s", "questions": {}})

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == EVALUATE_URL
    assert request.headers["authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {"model": JEV, "state": "s", "questions": {}}
    assert body == _answer(0.7)


def test_an_error_status_becomes_a_gateway_error_carrying_the_code() -> None:
    """`call_with_retry` decides on `status_code`, so it has to survive."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    with pytest.raises(GatewayError, match="rate limited") as caught:
        _evaluator(handler).evaluate({})
    assert caught.value.status_code == 429


def test_a_non_json_reply_is_an_error_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway timeout</html>")

    with pytest.raises(LlmError, match="JSON"):
        _evaluator(handler).evaluate({})


def test_the_key_never_appears_in_a_repr() -> None:
    """A dataclass prints every field by default, which would put the key in
    any traceback or log line that shows the evaluator."""
    assert "test-key" not in repr(GatewayEvaluator(api_key="test-key"))


# --- the pipeline ----------------------------------------------------------


class _Candidate:
    finish_reason = "STOP"


class _Usage:
    prompt_token_count = 900
    candidates_token_count = 120
    cached_content_token_count = 0
    thoughts_token_count = 0


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.text = json.dumps(payload)
        self.candidates = [_Candidate()]
        self.prompt_feedback = None
        self.usage_metadata = _Usage()


@dataclass
class FakeModels:
    responses: list[_Response]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate_content(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("Gemini was called more times than primed for")
        return self.responses.pop(0)


@dataclass
class FakeClient:
    models: FakeModels


def _email() -> EmailMessage:
    return EmailMessage(
        id="m1",
        thread_id="m1",
        subject="Design review",
        body_text="Thursday 4pm?",
        sender="sara@example.com",
        recipients=["me@example.com"],
        received_at=NOW,
    )


def _pipeline(gemini: list[_Response], evaluator: FakeEvaluator | None) -> ExtractionPipeline:
    return ExtractionPipeline(
        client=FakeClient(FakeModels(gemini)),
        classify_model=JEV,
        extraction_model="gemini-3.6-flash",
        owner_email="me@example.com",
        evaluator=evaluator,
    )


def test_jev_triage_never_calls_gemini_for_a_non_meeting() -> None:
    pipeline = _pipeline([], FakeEvaluator([_answer(0.04)]))

    result = pipeline(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert result.is_meeting is False
    assert pipeline.stats.classify_calls == 1
    assert pipeline.stats.extract_calls == 0


def test_jev_sees_what_the_gemini_classifier_would_have_seen() -> None:
    """Same grounding, same email block -- so a difference in the eval is the
    model's, not the prompt's."""
    evaluator = FakeEvaluator([_answer(0.04)])
    _pipeline([], evaluator)(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    state = evaluator.requests[0]["state"]
    assert prompts.user_content(_email(), now_utc=NOW, user_timezone="Asia/Karachi") in state


def test_a_jev_meeting_still_goes_to_gemini_for_extraction() -> None:
    client_responses = [_Response(EXTRACTION)]
    pipeline = _pipeline(client_responses, FakeEvaluator([_answer(0.96)]))

    result = pipeline(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    assert result.is_meeting is True
    assert result.title == "Design review"
    assert pipeline.stats.extract_calls == 1


def test_jev_without_an_evaluator_fails_with_the_fix_in_the_message() -> None:
    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY"):
        _pipeline([], None).classify(_email(), now_utc=NOW, user_timezone="Asia/Karachi")
