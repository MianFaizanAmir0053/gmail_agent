"""The tool-calling loop, against a fake client. No network, no API key.

The interesting properties are all about what happens when the model misbehaves:
calls a tool that does not exist, keeps calling forever, or never calls one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from app.contracts import EmailMessage
from app.extraction.llm import LlmError, structured_call
from app.extraction.payloads import ClassifyPayload
from app.extraction.pipeline import ExtractionPipeline
from app.extraction.prompts import EXTRACT_SYSTEM
from app.tools.search_context import SEARCH_CONTEXT_TOOL

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

ANSWER = {"is_meeting": True, "confidence": 0.9, "reasoning": "Explicit time."}

EXTRACTION = {
    "is_meeting": True,
    "title": "Platform review",
    "start_local": "2026-08-20T15:00:00",
    "end_local": "2026-08-20T16:00:00",
    "timezone": "Asia/Karachi",
    "attendees": ["ahmed.raza@northwind.example"],
    "location": None,
    "confidence": 0.9,
    "reasoning": "Thursday 3pm, address resolved from past threads.",
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
        self.prompt_token_count = 100
        self.candidates_token_count = 20
        self.cached_content_token_count = 0
        self.thoughts_token_count = 5


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


def _search_call(query: str = "Ahmed") -> _FunctionCall:
    return _FunctionCall("search_context", {"query": query})


def _dispatch_returning(payload: dict[str, Any]) -> Any:
    seen: list[tuple[str, dict[str, Any]]] = []

    def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        seen.append((name, args))
        return payload

    dispatch.seen = seen  # type: ignore[attr-defined]
    return dispatch


# --- the happy path ---------------------------------------------------------


def test_a_tool_call_is_answered_and_the_schema_still_holds() -> None:
    client = _client(_Response(calls=[_search_call()]), _Response(ANSWER))
    dispatch = _dispatch_returning({"results": [{"participants": ["a@x.com"]}]})

    result = structured_call(
        client,
        model="m",
        system="s",
        user="u",
        schema=ClassifyPayload,
        tools=[SEARCH_CONTEXT_TOOL],
        dispatch=dispatch,
    )

    assert result.parsed.is_meeting is True
    assert dispatch.seen == [("search_context", {"query": "Ahmed"})]


def test_usage_is_summed_across_turns() -> None:
    """One turn's tokens would under-report the cost of a searching extraction."""
    client = _client(_Response(calls=[_search_call()]), _Response(ANSWER))

    result = structured_call(
        client,
        model="m",
        system="s",
        user="u",
        schema=ClassifyPayload,
        tools=[SEARCH_CONTEXT_TOOL],
        dispatch=_dispatch_returning({"results": []}),
    )

    assert result.usage.input_tokens == 200
    assert result.usage.output_tokens == 40


def test_no_tool_call_costs_exactly_one_request() -> None:
    client = _client(_Response(ANSWER))

    structured_call(
        client,
        model="m",
        system="s",
        user="u",
        schema=ClassifyPayload,
        tools=[SEARCH_CONTEXT_TOOL],
        dispatch=_dispatch_returning({"results": []}),
    )

    assert len(client.models.calls) == 1


# --- bounds and misbehaviour ------------------------------------------------


def test_tools_are_withdrawn_on_the_final_turn() -> None:
    """Termination is a property of the request, not a hope about the model."""
    client = _client(
        _Response(calls=[_search_call()]),
        _Response(calls=[_search_call()]),
        _Response(ANSWER),
    )

    structured_call(
        client,
        model="m",
        system="s",
        user="u",
        schema=ClassifyPayload,
        tools=[SEARCH_CONTEXT_TOOL],
        dispatch=_dispatch_returning({"results": []}),
        max_tool_turns=2,
    )

    assert client.models.calls[-1]["config"].tools is None
    assert client.models.calls[0]["config"].tools is not None


def test_a_model_that_never_stops_searching_fails_loudly() -> None:
    client = _client(*[_Response(calls=[_search_call()]) for _ in range(4)])

    with pytest.raises(LlmError, match="kept calling tools"):
        structured_call(
            client,
            model="m",
            system="s",
            user="u",
            schema=ClassifyPayload,
            tools=[SEARCH_CONTEXT_TOOL],
            dispatch=_dispatch_returning({"results": []}),
            max_tool_turns=3,
        )


def test_tools_without_a_dispatch_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="no dispatch"):
        structured_call(
            _client(_Response(ANSWER)),
            model="m",
            system="s",
            user="u",
            schema=ClassifyPayload,
            tools=[SEARCH_CONTEXT_TOOL],
        )


# --- the pipeline seam ------------------------------------------------------


def _email() -> EmailMessage:
    return EmailMessage(
        id="m1",
        thread_id="m1",
        subject="Platform review",
        body_text="Ahmed asked to meet Thursday at 3.",
        sender="sara@example.com",
        recipients=["me@example.com"],
        received_at=NOW,
    )


def _pipeline(client: FakeClient) -> ExtractionPipeline:
    return ExtractionPipeline(
        client=client,
        classify_model="c",
        extraction_model="e",
        owner_email="me@example.com",
    )


def test_the_extraction_request_holds_no_tools() -> None:
    """No model that reads mail holds a tool (M18, decision 3): the request
    declares none, and the system instruction offers none."""
    client = _client(_Response(EXTRACTION))
    _pipeline(client).extract(_email(), now_utc=NOW, user_timezone="Asia/Karachi")

    request = client.models.calls[0]
    assert request["config"].tools is None
    assert request["config"].system_instruction == EXTRACT_SYSTEM
    assert isinstance(request["contents"], str)
