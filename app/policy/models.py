"""One metered path for model calls (M17, D5).

Every model client is built here, and nowhere else: a test fails if
`genai.Client(` or the Gateway's URL appears in any other module. The client
offers exactly what the code calls -- `models.generate_content`,
`models.embed_content`, and the Gateway's `evaluate` -- and no streaming, async
client or cache, so nothing can reach a model around the meter.

Each call asks the gate first (`app/policy/budget.py`), which may refuse it,
and is recorded after, in `model_spend`: model, message, token counts, cost.
Embeddings report no usage, so their tokens are estimated from characters,
four to a token, and the row says it is an estimate.

**Each prompt is bounded.** The text of every call is cut to
`MAX_PROMPT_CHARS`, longest part first, with a note saying so. One call's cost
is therefore bounded, and the gate's ceiling bounds one message's.

**Which message a call serves** is read from `CURRENT_MESSAGE`, which the graph
session sets around each run. Outside a run -- ingestion, the eval harness --
a call serves no message, and only the month's cap applies.
"""

from __future__ import annotations

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
from app.obs.pricing import cost_usd
from app.policy.budget import Gate, Spend

EVALUATE_URL = "https://ai-gateway.vercel.sh/v1/evaluate"
"""The Gateway's evaluation endpoint, the only place it is named (D5)."""

MAX_PROMPT_CHARS = 24_000
"""The most text one call may send. A prompt that would be longer is cut."""

CUT_NOTE = "\n[This text was cut to fit the agent's limit on one prompt.]"

CHARS_PER_TOKEN = 4
"""For an embedding's estimate: the endpoint reports no usage."""

CURRENT_MESSAGE: ContextVar[str | None] = ContextVar("current_message", default=None)
"""The message the running graph serves, so its calls count against its ceiling."""


def gate(settings: Settings, conn: psycopg.Connection) -> Gate:
    """The gate a session's clients share, on the session's connection."""
    return Gate(
        conn,
        cap_usd=Decimal(str(settings.monthly_budget_usd)),
        ceiling_usd=Decimal(str(settings.message_ceiling_usd)),
    )


def local_gate(settings: Settings) -> Gate:
    """A gate on a connection of its own, for a command-line tool: the eval
    harness, the model probe, a demo. Without a database there is no gate,
    and so no client."""
    return gate(settings, psycopg.connect(settings.database_url, autocommit=True))


def client(settings: Settings, meter: Gate) -> MeteredClient:
    """The only way a Gemini client is built."""
    from google import genai

    return MeteredClient(genai.Client(api_key=settings.gemini_api_key.get_secret_value()), meter)


def evaluator(settings: Settings, meter: Gate) -> MeteredEvaluator | None:
    """The Gateway's evaluator, when a key is configured."""
    key = settings.ai_gateway_api_key
    if key is None:
        return None
    return MeteredEvaluator(GatewayEvaluator(api_key=key.get_secret_value()), meter)


# --- Gemini -----------------------------------------------------------------------


@dataclass(slots=True)
class MeteredModels:
    inner: Any
    """The SDK's `client.models`."""
    meter: Gate
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def generate_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
        message_id = CURRENT_MESSAGE.get()
        self.meter.check(model, message_id)
        response = self.inner.generate_content(
            model=model, contents=bounded(contents), config=config
        )
        usage = _usage(response)
        self.meter.record(
            model,
            message_id,
            Spend(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_input_tokens,
                thinking_tokens=usage.thinking_tokens,
                cost_usd=_cost(model, self.clock(), usage),
            ),
        )
        return response

    def embed_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
        message_id = CURRENT_MESSAGE.get()
        self.meter.check(model, message_id)
        response = self.inner.embed_content(model=model, contents=contents, config=config)
        tokens = -(-_characters(contents) // CHARS_PER_TOKEN)
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
    inner: Any
    """The SDK's `genai.Client`."""
    meter: Gate
    models: MeteredModels = field(init=False)

    def __post_init__(self) -> None:
        self.models = MeteredModels(self.inner.models, self.meter)


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
    inner: GatewayEvaluator
    meter: Gate
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        model = str(request.get("model"))
        message_id = CURRENT_MESSAGE.get()
        self.meter.check(model, message_id)
        body = self.inner.evaluate(request)
        reported = body.get("usage")
        raw: dict[str, Any] = reported if isinstance(reported, dict) else {}
        usage = Usage(
            input_tokens=_count(raw.get("inputTokens")),
            output_tokens=_count(raw.get("outputTokens")),
        )
        self.meter.record(
            model,
            message_id,
            Spend(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=_cost(model, self.clock(), usage),
            ),
        )
        return body


# --- bounding a prompt ---------------------------------------------------------------------


def bounded(contents: Any, limit: int = MAX_PROMPT_CHARS) -> Any:
    """`contents` with at most `limit` characters of text, cut longest part
    first, with `CUT_NOTE` where a part was cut. Structure is kept: tool calls
    and their answers stay paired, and only text is shortened."""
    while (excess := _characters(contents) - limit) > 0:
        longest = max(_texts(contents), key=len, default="")
        if len(longest) <= len(CUT_NOTE):
            break  # nothing left worth cutting
        keep = max(len(longest) - excess - len(CUT_NOTE), 0)
        contents = _replace_text(contents, longest, longest[:keep] + CUT_NOTE)
    return contents


def _texts(contents: Any) -> list[str]:
    if isinstance(contents, str):
        return [contents]
    if isinstance(contents, list | tuple):
        return [text for item in contents for text in _texts(item)]
    parts = getattr(contents, "parts", None)
    if parts is not None:
        return _texts(list(parts))
    text = getattr(contents, "text", None)
    return [text] if isinstance(text, str) else []


def _characters(contents: Any) -> int:
    return sum(len(text) for text in _texts(contents))


def _replace_text(contents: Any, old: str, new: str) -> Any:
    """`contents` with the first text part equal to `old` replaced by `new`."""
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
            return item.model_copy(update={"parts": walk(list(parts))})
        if getattr(item, "text", None) == old:
            done[0] = True
            return item.model_copy(update={"text": new})
        return item

    return walk(contents)


# --- small helpers ----------------------------------------------------------------------------


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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


def _cost(model: str, at: datetime, usage: Usage) -> Decimal:
    """Priced by the gate's table; a model the gate let through has a rate."""
    cost = cost_usd(
        model,
        at=at,
        input_tokens=usage.input_tokens,
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
