"""One metered path for model calls (M17, D5)."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from app.contracts import EmailMessage
from app.extraction.pipeline import ExtractionPipeline
from app.policy import models
from app.policy.budget import Spend, UnpricedModelError

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)


# --- nothing reaches a model around the meter ------------------------------------------

HOSTS = ("ai-gateway.vercel.sh", "generativelanguage.googleapis.com")


def _where(path: Path) -> str:
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name


def _model_clients(path: Path) -> list[str]:
    """Where a module could reach a model around the meter: a `google.genai`
    client however it is spelled, either host's URL, or the raw client held
    inside the wrapper."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    genai = {"genai", "google.genai"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            genai |= {a.asname or a.name for a in node.names if a.name.startswith("google.genai")}
        elif isinstance(node, ast.ImportFrom) and node.module == "google":
            genai |= {a.asname or a.name for a in node.names if a.name == "genai"}

    found = []
    for node in ast.walk(tree):
        if (
            (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("google.genai")
                and any(a.name == "Client" for a in node.names)
            )
            or (
                isinstance(node, ast.Attribute)
                and node.attr == "Client"
                and ast.unparse(node.value) in genai | {f"{name}.client" for name in genai}
            )
            or (isinstance(node, ast.Attribute) and node.attr == "_inner")
            or (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(host in node.value for host in HOSTS)
            )
        ):
            found.append(f"{_where(path)}:{node.lineno}")
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


def test_the_scan_sees_every_spelling(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "\n".join(
            [
                'client = genai.Client(api_key="k")',
                'client = google.genai.Client(api_key="k")',
                'client = genai.client.Client(api_key="k")',
                "from google.genai import Client",
                'URL = "https://ai-gateway.vercel.sh/v1/x"',
                'URL = "https://generativelanguage.googleapis.com/v1beta/models"',
                "raw = metered._inner",
                "import google.genai as g",
                'client = g.Client(api_key="k")',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    lines = sorted(int(where.rsplit(":", 1)[1]) for where in _model_clients(sample))

    assert lines == [1, 2, 3, 4, 5, 6, 7, 9]


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


def _usage_metadata(prompt: int = 1_000_000, cached: int = 0) -> Any:
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=0,
        cached_content_token_count=cached,
        thoughts_token_count=0,
    )


@dataclass
class FakeSdkModels:
    calls: list[dict[str, Any]] = field(default_factory=list)
    usage: Any = field(default_factory=_usage_metadata)

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(usage_metadata=self.usage, candidates=[])

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


def test_a_call_that_reports_no_usage_is_estimated_not_free() -> None:
    """A call billed but recorded at nothing would let the cap fail open."""
    gate = FakeGate()
    metered, sdk = _metered(gate)
    sdk.usage = None

    metered.generate_content(
        model="gemini-3.6-flash",
        contents="x" * 400,
        config=types.GenerateContentConfig(system_instruction="y" * 400),
    )

    [(_, _, spend)] = gate.recorded
    assert (spend.input_tokens, spend.estimated) == (200, True)
    assert spend.cost_usd > Decimal(0)


def test_more_cached_tokens_than_prompt_tokens_still_records_the_call() -> None:
    """The price table refuses that reading. The call was made and billed, so
    its row must still be written, priced as a prompt at least that long."""
    gate = FakeGate()
    metered, sdk = _metered(gate)
    sdk.usage = _usage_metadata(prompt=10, cached=1_000_000)

    metered.generate_content(model="gemini-3.6-flash", contents="hello")

    [(_, _, spend)] = gate.recorded
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
    """No stream, no async client, no cache, and no public way to the raw
    client: nothing around the meter."""
    gate = FakeGate()
    sdk = SimpleNamespace(models=FakeSdkModels(), aio=object(), caches=object())
    client = models.MeteredClient(sdk, gate)  # type: ignore[arg-type]

    assert not hasattr(client, "aio") and not hasattr(client, "caches")
    assert not hasattr(client, "inner") and not hasattr(client.models, "inner")
    assert not hasattr(client.models, "generate_content_stream")


def test_the_probe_lists_what_the_key_advertises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Listing calls no model and costs nothing, and the wrapper offers no
    listing: it is built here, like every client, unmetered."""
    from app.config import Settings
    from app.extraction import models as probe

    listed = [
        SimpleNamespace(
            name="models/gemini-3.6-flash",
            supported_actions=["generateContent"],
            display_name="Flash",
        )
    ]

    class FakeGenaiClient:
        def __init__(self, api_key: str) -> None:
            self.models = SimpleNamespace(list=lambda: iter(listed))

    monkeypatch.setattr("google.genai.Client", FakeGenaiClient)
    settings = Settings(
        _env_file=None, database_url="postgresql://localhost/test", gemini_api_key="k"
    )

    probe.list_models(models.advertised(settings))

    assert "gemini-3.6-flash" in capsys.readouterr().out


@dataclass
class FakeGateway:
    usage: dict[str, Any] | None = field(
        default_factory=lambda: {"inputTokens": 275, "outputTokens": 20}
    )
    requests: list[dict[str, Any]] = field(default_factory=list)

    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        body: dict[str, Any] = {"answers": {"calendar_event": {"probability": 0.9}}}
        if self.usage is not None:
            body["usage"] = self.usage
        return body


def test_the_gateways_evaluation_is_metered_by_its_reported_usage() -> None:
    gate = FakeGate()
    metered = models.MeteredEvaluator(FakeGateway(), gate)  # type: ignore[arg-type]

    metered.evaluate({"model": "typesafe-ai/jev", "state": "..."})

    [(model, _, spend)] = gate.recorded
    assert (model, spend.input_tokens, spend.output_tokens) == ("typesafe-ai/jev", 275, 20)
    assert not spend.estimated


def test_an_evaluation_that_reports_no_usage_is_estimated() -> None:
    gate = FakeGate()
    metered = models.MeteredEvaluator(FakeGateway(usage=None), gate)  # type: ignore[arg-type]

    metered.evaluate({"model": "typesafe-ai/jev", "state": "x" * 4_000})

    [(_, _, spend)] = gate.recorded
    assert spend.estimated and spend.input_tokens > 1_000


def test_the_gateways_state_is_held_to_the_bound() -> None:
    gateway = FakeGateway()
    metered = models.MeteredEvaluator(gateway, FakeGate())  # type: ignore[arg-type]
    request = {"model": "typesafe-ai/jev", "state": "x" * 30_000, "questions": {"q": "?"}}

    metered.evaluate(request)

    [sent] = gateway.requests
    assert models.CUT_NOTE in sent["state"]
    assert len(str(sent["state"])) < models.MAX_PROMPT_CHARS
    assert sent["questions"] == {"q": "?"}


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
    assert models.CUT_NOTE in parts[1].text
    assert sum(len(part.text) for part in parts) <= 500


def test_a_cut_keeps_both_ends_of_the_text() -> None:
    """The start of an email and what follows it are what matter most."""
    text = "START" + "x" * 1_000 + "END"

    cut = models.bounded(text, limit=300)

    assert cut.startswith("START") and cut.endswith("END") and models.CUT_NOTE in cut
    assert len(cut) <= 300


def test_tool_calls_and_answers_are_kept_whole_and_counted() -> None:
    call = types.Part(function_call=types.FunctionCall(name="search_context", args={"q": "x"}))
    answer = types.Part(
        function_response=types.FunctionResponse(
            name="search_context", response={"hits": ["h" * 300]}
        )
    )
    contents = [
        types.Content(role="user", parts=[types.Part(text="e" * 900)]),
        types.Content(role="model", parts=[call]),
        types.Content(role="user", parts=[answer]),
    ]

    cut = models.bounded(contents, limit=600)

    assert cut[1].parts[0].function_call.name == "search_context"
    assert cut[2].parts[0].function_response.response == {"hits": ["h" * 300]}
    assert len(cut[0].parts[0].text) < 300  # the search results took their share
    assert models._characters(cut) <= 600


def test_a_models_own_turn_is_never_cut() -> None:
    """It carries the model's thought signature; the user's text gives way."""
    contents = [
        types.Content(role="user", parts=[types.Part(text="u" * 400)]),
        types.Content(role="model", parts=[types.Part(text="m" * 900)]),
    ]

    cut = models.bounded(contents, limit=1_000)

    assert cut[1].parts[0].text == "m" * 900
    assert models.CUT_NOTE in cut[0].parts[0].text


def test_the_wrapper_sends_the_bounded_prompt() -> None:
    gate = FakeGate()
    metered, sdk = _metered(gate)

    metered.generate_content(
        model="gemini-3.6-flash", contents="x" * (models.MAX_PROMPT_CHARS + 50)
    )

    sent = sdk.calls[0]["contents"]
    assert len(sent) <= models.MAX_PROMPT_CHARS and models.CUT_NOTE in sent


# --- what production sends: one string, an email and what follows it ------------------------


def _long_email() -> EmailMessage:
    return EmailMessage(
        id="m1",
        thread_id="m1",
        subject="Design review",
        body_text="Can we meet Thursday at 10? " + "padding " * 4_000,
        sender="sara@example.com",
        recipients=["me@example.com"],
        received_at=NOW,
    )


def _pipeline() -> ExtractionPipeline:
    return ExtractionPipeline(
        client=object(),  # type: ignore[arg-type]
        classify_model="m",
        extraction_model="m",
    )


def test_an_owners_correction_survives_a_long_email() -> None:
    """The prompt is one string, the correction at its end. Cut from the end,
    the correction went and the Edit silently did nothing."""
    pipeline = _pipeline()
    correction = "Correction from the user, which takes precedence:\nmove it to 3pm"

    sent = models.bounded(pipeline._user(_long_email(), NOW, "UTC", correction))

    assert sent.rstrip().endswith("move it to 3pm")
    assert "Can we meet Thursday at 10?" in sent and "Subject: Design review" in sent
    assert models.CUT_NOTE in sent and len(sent) <= models.MAX_PROMPT_CHARS


def test_a_short_email_is_sent_exactly_as_before() -> None:
    """The frozen extraction baseline was measured on these exact bytes."""
    pipeline = _pipeline()
    email = _long_email().model_copy(update={"body_text": "Thursday at 10?"})

    from app.extraction import prompts

    expected = (
        f"{prompts.grounding_block(NOW, 'UTC')}\n{prompts.email_block(email)}\nfix the time\n"
    )
    assert pipeline._user(email, NOW, "UTC", "fix the time") == expected
