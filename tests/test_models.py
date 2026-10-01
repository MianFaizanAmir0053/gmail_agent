"""One metered path for model calls (M17, D5)."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from app.policy import models
from app.policy.budget import Spend, UnpricedModelError

ROOT = Path(__file__).resolve().parent.parent


# --- nothing reaches a model around the meter ------------------------------------------


def _model_clients(path: Path) -> list[str]:
    """`genai.Client(...)` calls and the Gateway's URL, wherever they appear."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Client"
            and ast.unparse(node.func.value) == "genai"
        ) or (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "ai-gateway.vercel.sh" in node.value
        ):
            found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    return found


def test_every_model_client_is_built_by_the_metered_path() -> None:
    """Like `tests/test_one_resumer.py`: a client built anywhere else could
    spend without the gate ever hearing of it."""
    found = [
        call
        for path in sorted((ROOT / "app").rglob("*.py"))
        if path.relative_to(ROOT).as_posix() != "app/policy/models.py"
        for call in _model_clients(path)
    ]
    assert found == []


def test_the_scan_sees_a_client_and_the_url(tmp_path: Path) -> None:
    sample = ROOT / "tests" / "_client_sample.py"
    try:
        sample.write_text(
            'client = genai.Client(api_key="k")\nURL = "https://ai-gateway.vercel.sh/v1/x"\n',
            encoding="utf-8",
        )
        assert sorted(c.rsplit(":", 1)[1] for c in _model_clients(sample)) == ["1", "2"]
    finally:
        sample.unlink()


# --- the wrapper ---------------------------------------------------------------


@dataclass
class FakeGate:
    refuse: Exception | None = None
    checked: list[tuple[str, str | None]] = field(default_factory=list)
    recorded: list[tuple[str, str | None, Spend]] = field(default_factory=list)

    def check(self, model: str, message_id: str | None) -> None:
        self.checked.append((model, message_id))
        if self.refuse is not None:
            raise self.refuse

    def record(self, model: str, message_id: str | None, spend: Spend) -> None:
        self.recorded.append((model, message_id, spend))


@dataclass
class FakeSdkModels:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            usage_metadata=SimpleNamespace(
                prompt_token_count=1_000_000,
                candidates_token_count=0,
                cached_content_token_count=0,
                thoughts_token_count=0,
            )
        )

    def embed_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(embeddings=[])


def _metered(gate: FakeGate) -> tuple[models.MeteredModels, FakeSdkModels]:
    sdk = FakeSdkModels()
    return models.MeteredModels(sdk, gate), sdk  # type: ignore[arg-type]


def test_a_refused_call_never_reaches_the_model() -> None:
    gate = FakeGate(refuse=UnpricedModelError("no rate"))
    metered, sdk = _metered(gate)

    with pytest.raises(UnpricedModelError):
        metered.generate_content(model="gemini-2.5-pro", contents="hello")

    assert sdk.calls == [] and gate.recorded == []


def test_a_call_is_recorded_with_its_tokens_and_its_cost() -> None:
    gate = FakeGate()
    metered, _ = _metered(gate)

    metered.generate_content(model="gemini-3.6-flash", contents="hello")

    [(model, message_id, spend)] = gate.recorded
    assert (model, message_id) == ("gemini-3.6-flash", None)
    assert (spend.input_tokens, spend.estimated) == (1_000_000, False)
    assert spend.cost_usd > Decimal(0)


def test_a_call_counts_against_the_message_it_serves() -> None:
    gate = FakeGate()
    metered, _ = _metered(gate)
    serving = models.CURRENT_MESSAGE.set("m1")
    try:
        metered.generate_content(model="gemini-3.6-flash", contents="hello")
    finally:
        models.CURRENT_MESSAGE.reset(serving)

    assert gate.checked == [("gemini-3.6-flash", "m1")]
    assert gate.recorded[0][1] == "m1"


def test_an_embedding_is_priced_from_its_characters_and_marked_an_estimate() -> None:
    """The endpoint reports no usage: four characters a token."""
    gate = FakeGate()
    metered, _ = _metered(gate)
    contents = [types.Content(parts=[types.Part(text="x" * 9)])]

    metered.embed_content(model="gemini-embedding-001", contents=contents)

    [(_, _, spend)] = gate.recorded
    assert (spend.input_tokens, spend.estimated) == (3, True)


def test_the_client_offers_only_the_two_calls() -> None:
    """No stream, no async client, no cache: nothing around the meter."""
    gate = FakeGate()
    sdk = SimpleNamespace(models=FakeSdkModels(), aio=object(), caches=object())
    client = models.MeteredClient(sdk, gate)  # type: ignore[arg-type]

    assert not hasattr(client, "aio") and not hasattr(client, "caches")
    assert not hasattr(client.models, "generate_content_stream")


def test_the_gateways_evaluation_is_metered_by_its_reported_usage() -> None:
    gate = FakeGate()

    @dataclass
    class Gateway:
        def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
            return {"usage": {"inputTokens": 275, "outputTokens": 20}, "answers": {}}

    metered = models.MeteredEvaluator(Gateway(), gate)  # type: ignore[arg-type]

    metered.evaluate({"model": "typesafe-ai/jev", "state": "..."})

    [(model, _, spend)] = gate.recorded
    assert (model, spend.input_tokens, spend.output_tokens) == ("typesafe-ai/jev", 275, 20)


# --- each prompt is bounded ------------------------------------------------------------


def test_a_prompt_under_the_bound_is_sent_as_it_is() -> None:
    assert models.bounded("short", limit=100) == "short"


def test_a_long_prompt_is_cut_longest_part_first_with_a_note() -> None:
    email = "e" * 900
    contents = [
        types.Content(role="user", parts=[types.Part(text="instructions"), types.Part(text=email)])
    ]

    cut = models.bounded(contents, limit=500)

    parts = cut[0].parts
    assert parts[0].text == "instructions"
    assert parts[1].text.endswith(models.CUT_NOTE)
    assert sum(len(part.text) for part in parts) <= 500


def test_tool_calls_and_answers_are_kept_whole() -> None:
    call = types.Part(function_call=types.FunctionCall(name="search_context", args={"q": "x"}))
    answer = types.Part(
        function_response=types.FunctionResponse(name="search_context", response={"hits": []})
    )
    contents = [
        types.Content(role="user", parts=[types.Part(text="e" * 900)]),
        types.Content(role="model", parts=[call]),
        types.Content(role="user", parts=[answer]),
    ]

    cut = models.bounded(contents, limit=500)

    assert cut[1].parts[0].function_call.name == "search_context"
    assert cut[2].parts[0].function_response.name == "search_context"
    assert models._characters(cut) <= 500


def test_the_wrapper_sends_the_bounded_prompt() -> None:
    gate = FakeGate()
    metered, sdk = _metered(gate)

    metered.generate_content(
        model="gemini-3.6-flash", contents="x" * (models.MAX_PROMPT_CHARS + 50)
    )

    sent = sdk.calls[0]["contents"]
    assert len(sent) <= models.MAX_PROMPT_CHARS and sent.endswith(models.CUT_NOTE)
