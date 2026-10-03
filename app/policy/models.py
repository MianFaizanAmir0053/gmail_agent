"""One metered path for model calls (M17, D5).

Every model client is built here, and nowhere else: a test fails if a
`google.genai` client, either model host's URL, or the raw client held inside
the wrapper appears in any other module. The client offers exactly what the
code calls -- `models.generate_content`, `models.embed_content`, and the
Gateway's `evaluate` -- and no streaming, async client or cache, so nothing can
reach a model around the meter.

Each call asks the gate first (`app/policy/budget.py`), which may refuse it,
and is recorded after, in `model_spend`: model, message, token counts, cost.
Embeddings report no usage, so their tokens are estimated from characters,
four to a token, and the row says it is an estimate. So is any other call
that comes back without usage: an unreported call must not count as free.

**Each prompt is bounded** to `MAX_PROMPT_CHARS`, tool calls and their answers
counted, with a note where text was cut. Callers cut first, where they know
what can give way: an email's body, never what follows it
(`app/extraction/prompts.py`). `bounded` is the backstop for whatever still
runs over, such as a prompt grown by search results. One call's cost is
therefore bounded, and the gate's ceiling bounds one message's.

**Which message a call serves** is read from `CURRENT_MESSAGE`, which the graph
session sets around each run. Outside a run -- ingestion, the eval harness --
a call serves no message, and only the month's cap applies.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import psycopg

from app.config import Settings
from app.extraction.evaluation import GatewayError
from app.extraction.llm import LlmError, Usage
from app.extraction.prompts import CUT_NOTE
from app.obs.pricing import cost_usd, rate_at
from app.policy.budget import Gate, Spend

EVALUATE_URL = "https://ai-gateway.vercel.sh/v1/evaluate"
"""The Gateway's evaluation endpoint, the only place it is named (D5)."""

MAX_PROMPT_CHARS = 24_000
"""The most one call may send, in characters. A prompt that would be longer is cut."""

CHARS_PER_TOKEN = 4
"""For an estimate, when a call reports no usage."""

LOCAL_CONNECT_TIMEOUT = 10
"""Seconds a command-line tool's gate waits for the database. Without one, an
unreachable host holds the caller for minutes on Windows (`app/store/db.py`)."""

CURRENT_MESSAGE: ContextVar[str | None] = ContextVar("current_message", default=None)
"""The message the running graph serves, so its calls count against its ceiling."""

__all__ = [
    "CHARS_PER_TOKEN",
    "CURRENT_MESSAGE",
    "CUT_NOTE",
    "EVALUATE_URL",
    "MAX_PROMPT_CHARS",
    "GatewayEvaluator",
    "MeteredClient",
    "MeteredEvaluator",
    "MeteredModels",
    "advertised",
    "bounded",
    "client",
    "evaluator",
    "gate",
    "in_use",
    "local_gate",
    "unpriced",
]


def gate(settings: Settings, conn: psycopg.Connection) -> Gate:
    """The gate a session's clients share, on the session's connection."""
    return Gate(
        conn,
        cap_usd=Decimal(str(settings.monthly_budget_usd)),
        ceiling_usd=Decimal(str(settings.message_ceiling_usd)),
        models_in_use=tuple(in_use(settings)),
    )


def local_gate(settings: Settings) -> Gate:
    """A gate on a connection of its own, for a command-line tool: the eval
    harness, the model probe, a demo. Each row commits as it is written, so a
    run that fails half way has still recorded what it spent. The connection
    closes when the process exits; a long-running caller closes `gate.conn`
    itself, as the ingestion job does. Without a database there is no gate,
    and so no client."""
    return gate(
        settings,
        psycopg.connect(
            settings.database_url, autocommit=True, connect_timeout=LOCAL_CONNECT_TIMEOUT
        ),
    )


def client(settings: Settings, meter: Gate) -> MeteredClient:
    """The only way a Gemini client is built."""
    from google import genai

    return MeteredClient(genai.Client(api_key=settings.gemini_api_key.get_secret_value()), meter)


def advertised(settings: Settings) -> list[Any]:
    """The models the key's listing advertises, for the model probe. Listing
    calls no model and costs nothing, so it is not metered; it is here because
    every client is."""
    from google import genai

    return list(genai.Client(api_key=settings.gemini_api_key.get_secret_value()).models.list())


def in_use(settings: Settings) -> list[str]:
    """The models the running app calls. A feature that is off calls none:
    the embedding model only with search or ingestion on (D5)."""
    used = [settings.classify_model, settings.extraction_model]
    if settings.search_context_enabled or settings.ingest_enabled:
        used.append(settings.embedding_model)
    return used


def unpriced(settings: Settings, at: datetime) -> list[str]:
    """The models in use with no rate at `at`. The gate refuses every call
    to one, so whatever needs it has stopped: `/health` says so with a 503."""
    return sorted({model for model in in_use(settings) if rate_at(model, at) is None})


def evaluator(settings: Settings, meter: Gate) -> MeteredEvaluator | None:
    """The Gateway's evaluator, when a key is configured."""
    key = settings.ai_gateway_api_key
    if key is None:
        return None
    return MeteredEvaluator(GatewayEvaluator(api_key=key.get_secret_value()), meter)


# --- Gemini -----------------------------------------------------------------------


@dataclass(slots=True)
class MeteredModels:
    _inner: Any
    """The SDK's `client.models`. Private: a call through it would pass the
    meter, and the call-site test fails any module outside this one that
    reaches for it."""
    meter: Gate
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def generate_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
        message_id = CURRENT_MESSAGE.get()
        self.meter.check(model, message_id)
        sent = bounded(contents)
        response = self._inner.generate_content(model=model, contents=sent, config=config)
        usage, estimated = _usage(response), False
        if usage.input_tokens == 0:
            # A real call always has a prompt, so this one reported no usage.
            # Estimated from characters, like an embedding, so that it still
            # counts against the cap.
            system = getattr(config, "system_instruction", None)
            usage = Usage(
                input_tokens=_tokens(_characters(sent) + _characters(system)),
                output_tokens=_tokens(_answer_characters(response)),
            )
            estimated = True
        self.meter.record(
            model,
            message_id,
            Spend(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_input_tokens,
                thinking_tokens=usage.thinking_tokens,
                cost_usd=_cost(model, self.clock(), usage),
                estimated=estimated,
            ),
        )
        return response

    def embed_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
        message_id = CURRENT_MESSAGE.get()
        self.meter.check(model, message_id)
        response = self._inner.embed_content(model=model, contents=contents, config=config)
        tokens = _tokens(_characters(contents))
        estimate = Usage(input_tokens=tokens)
        self.meter.record(
            model,
            message_id,
            Spend(
                input_tokens=tokens,
                cost_usd=_cost(model, self.clock(), estimate),
                estimated=True,
            ),
        )
        return response


@dataclass(slots=True)
class MeteredClient:
    _inner: Any
    """The SDK's `genai.Client`. Private, like `MeteredModels._inner`."""
    meter: Gate
    models: MeteredModels = field(init=False)

    def __post_init__(self) -> None:
        self.models = MeteredModels(self._inner.models, self.meter)


# --- the Gateway ----------------------------------------------------------------------


@dataclass(slots=True)
class GatewayEvaluator:
    """The Gateway's evaluate endpoint, over plain HTTP."""

    api_key: str = field(repr=False)
    """Kept out of the repr: a dataclass prints every field by default, and this
    one would otherwise land in any traceback or log line showing the object."""
    timeout: float = 20.0
    transport: httpx.BaseTransport | None = None
    """Tests substitute `httpx.MockTransport`; production leaves it unset."""

    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(transport=self.transport, timeout=self.timeout) as http:
            response = http.post(
                EVALUATE_URL,
                json=request,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        if response.is_error:
            raise GatewayError(response.status_code, _error_message(response))
        try:
            body = response.json()
        except ValueError as exc:
            raise LlmError(f"AI Gateway returned non-JSON: {response.text[:200]}") from exc
        if not isinstance(body, dict):
            raise LlmError(f"AI Gateway returned JSON that is not an object: {body!r}")
        return body


@dataclass(slots=True)
class MeteredEvaluator:
    _inner: GatewayEvaluator
    """Private, like `MeteredModels._inner`."""
    meter: Gate
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        model = str(request.get("model"))
        message_id = CURRENT_MESSAGE.get()
        self.meter.check(model, message_id)
        sent = _bounded_request(request)
        body = self._inner.evaluate(sent)
        reported = body.get("usage")
        raw: dict[str, Any] = reported if isinstance(reported, dict) else {}
        usage = Usage(
            input_tokens=_count(raw.get("inputTokens")),
            output_tokens=_count(raw.get("outputTokens")),
        )
        estimated = usage.input_tokens == 0
        if estimated:
            # No usage reported, or under other names: estimated, as above.
            usage = Usage(
                input_tokens=_tokens(len(json.dumps(sent, default=str))),
                output_tokens=_tokens(len(json.dumps(body.get("answers"), default=str))),
            )
        self.meter.record(
            model,
            message_id,
            Spend(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=_cost(model, self.clock(), usage),
                estimated=estimated,
            ),
        )
        return body


def _bounded_request(request: dict[str, Any]) -> dict[str, Any]:
    """The request with its state cut so that the whole stays within the bound.
    The questions are short, and are kept whole."""
    state = request.get("state")
    if not isinstance(state, str):
        return request
    others = len(json.dumps({k: v for k, v in request.items() if k != "state"}, default=str))
    return {**request, "state": bounded(state, limit=max(MAX_PROMPT_CHARS - others, 0))}


# --- bounding a prompt ---------------------------------------------------------------------


def bounded(contents: Any, limit: int = MAX_PROMPT_CHARS) -> Any:
    """`contents` with at most `limit` characters in all, tool calls and their
    answers counted, and `CUT_NOTE` where text was cut.

    Only the user's text is shortened: longest part first, and from its
    middle, keeping two thirds of what stays from its start and a third from
    its end. A part that is an email followed by an owner's correction, or by
    the proposal under review, keeps both ends. A model's own turn is never
    touched, since it carries the model's thought signature, and neither is a
    tool call or its answer, which come in pairs.
    """
    while (excess := _characters(contents) - limit) > 0:
        longest = max(_cuttable(contents), key=len, default="")
        if len(longest) <= len(CUT_NOTE):
            break  # nothing left worth cutting
        keep = max(len(longest) - excess - len(CUT_NOTE), 0)
        tail = keep // 3
        cut = longest[: keep - tail] + CUT_NOTE + longest[len(longest) - tail :]
        contents = _replace_text(contents, longest, cut)
    return contents


def _is_model_turn(item: Any) -> bool:
    return getattr(item, "role", None) == "model"


def _cuttable(contents: Any) -> list[str]:
    """The text a cut may shorten: the user's, never a model's own turn."""
    if isinstance(contents, str):
        return [contents]
    if isinstance(contents, list | tuple):
        return [text for item in contents for text in _cuttable(item)]
    parts = getattr(contents, "parts", None)
    if parts is not None:
        return [] if _is_model_turn(contents) else _cuttable(list(parts))
    text = getattr(contents, "text", None)
    return [text] if isinstance(text, str) else []


def _characters(contents: Any) -> int:
    """Every character a call sends: text, tool calls and their answers."""
    if contents is None:
        return 0
    if isinstance(contents, str):
        return len(contents)
    if isinstance(contents, list | tuple):
        return sum(_characters(item) for item in contents)
    parts = getattr(contents, "parts", None)
    if parts is not None:
        return _characters(list(parts))
    text = getattr(contents, "text", None)
    total = len(text) if isinstance(text, str) else 0
    for name in ("function_call", "function_response"):
        value = getattr(contents, name, None)
        if value is not None:
            total += len(value.model_dump_json(exclude_none=True))
    return total


def _replace_text(contents: Any, old: str, new: str) -> Any:
    """`contents` with the first of the user's text parts equal to `old`
    replaced by `new`."""
    done = [False]

    def walk(item: Any) -> Any:
        if done[0]:
            return item
        if isinstance(item, str):
            if item == old:
                done[0] = True
                return new
            return item
        if isinstance(item, list | tuple):
            return type(item)(walk(child) for child in item)
        parts = getattr(item, "parts", None)
        if parts is not None:
            if _is_model_turn(item):
                return item
            return item.model_copy(update={"parts": walk(list(parts))})
        if getattr(item, "text", None) == old:
            done[0] = True
            return item.model_copy(update={"text": new})
        return item

    return walk(contents)


# --- small helpers ----------------------------------------------------------------------------


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _tokens(characters: int) -> int:
    """An estimate: `CHARS_PER_TOKEN` characters a token, rounded up."""
    return -(-characters // CHARS_PER_TOKEN)


def _usage(response: Any) -> Usage:
    raw = getattr(response, "usage_metadata", None)
    if raw is None:
        return Usage()
    return Usage(
        input_tokens=_count(getattr(raw, "prompt_token_count", 0)),
        output_tokens=_count(getattr(raw, "candidates_token_count", 0)),
        cached_input_tokens=_count(getattr(raw, "cached_content_token_count", 0)),
        thinking_tokens=_count(getattr(raw, "thoughts_token_count", 0)),
    )


def _answer_characters(response: Any) -> int:
    candidates = getattr(response, "candidates", None) or []
    return _characters(getattr(candidates[0], "content", None)) if candidates else 0


def _cost(model: str, at: datetime, usage: Usage) -> Decimal:
    """Priced by the gate's table; a model the gate let through has a rate.

    Cached tokens are part of the prompt, so a report of more cached tokens
    than prompt tokens is read as a prompt at least that long: the price
    table refuses the other reading, and a call priced at nothing would not
    count against the cap.
    """
    cost = cost_usd(
        model,
        at=at,
        input_tokens=max(usage.input_tokens, usage.cached_input_tokens),
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cached_input_tokens,
        thinking_tokens=usage.thinking_tokens,
    )
    return cost if cost is not None else Decimal(0)


def _error_message(response: httpx.Response) -> str:
    """The gateway's own words when it gives some, the raw text when not."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    return response.text[:300]
