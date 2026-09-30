"""One structured call to Gemini, with the failure modes handled.

`Usage` and `Completion` are deliberately provider-neutral: M08's cost tracking
and the pipeline's stats depend on them, and neither should care which vendor
answered. Everything vendor-specific is confined to this module.

Two failure modes get their own exception because they are not the same thing
and must not be retried the same way:

- **Blocked** -- safety filters refused, either on the prompt (`prompt_feedback`)
  or the response (`finish_reason`). Retrying the identical request is pointless.
- **Truncated** -- `MAX_TOKENS` mid-JSON. The payload is unparseable rather than
  merely short, and a bigger budget genuinely fixes it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from app.extraction.payloads import response_json_schema
from app.obs.trace import record_llm_usage

BLOCKING_FINISH_REASONS = frozenset(
    {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}
)

TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
"""Worth retrying. A 503 "high demand" is routine on popular models and must not
be allowed to end a whole eval run; a 400 schema error never improves on retry."""


DAILY_QUOTA_MARKERS = ("PerDay", "per day")
"""A per-day quota is not transient in any useful sense.

Some models allow as few as 20 requests per *day*; retrying that wall just burns
minutes before failing anyway, so it fails fast with an actionable message.

Match on `PerDay` only. An earlier version also matched `FreeTier`, which
appears in *both* `GenerateRequestsPerDayPerProjectPerModel-FreeTier` and
`GenerateRequestsPerMinutePerProjectPerModel-FreeTier` -- so a 5-per-minute
limit, which clears in twenty seconds, was being given up on immediately.
"""


def _is_transient(exc: BaseException) -> bool:
    # Duck-typed rather than catching google.genai.errors, so the retry path
    # stays testable with a fake that raises a plain object carrying `.code`.
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code not in TRANSIENT_STATUS_CODES:
        return False
    return not any(marker in str(exc) for marker in DAILY_QUOTA_MARKERS)


def _server_retry_delay(exc: BaseException) -> float | None:
    """The server's own `retryDelay` hint, when it gives one.

    Better than guessing: a fixed backoff either overshoots (wasting wall clock)
    or undershoots (burning another attempt for nothing).
    """
    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", str(exc))
    return float(match.group(1)) if match else None


class LlmError(RuntimeError):
    """The call completed but produced nothing usable."""


class BlockedError(LlmError):
    """Safety filters refused the prompt or the response."""


class TruncatedError(LlmError):
    """Hit the output token cap mid-JSON."""


class ModelsResource(Protocol):
    def generate_content(self, **kwargs: Any) -> Any: ...


class GenaiLike(Protocol):
    """Just enough of the SDK surface to allow a fake in tests."""

    @property
    def models(self) -> ModelsResource: ...


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    """The whole prompt, as `prompt_token_count` reports it -- cached share included."""
    output_tokens: int = 0
    cached_input_tokens: int = 0
    """The share of `input_tokens` served from cache, not tokens on top of it."""
    thinking_tokens: int = 0

    @property
    def cache_hit(self) -> bool:
        return self.cached_input_tokens > 0


@dataclass(frozen=True, slots=True)
class Completion[M: BaseModel]:
    parsed: M
    usage: Usage
    model: str


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int) else 0


def _usage(response: Any) -> Usage:
    raw = getattr(response, "usage_metadata", None)
    if raw is None:
        return Usage()
    return Usage(
        input_tokens=_int(getattr(raw, "prompt_token_count", 0)),
        output_tokens=_int(getattr(raw, "candidates_token_count", 0)),
        cached_input_tokens=_int(getattr(raw, "cached_content_token_count", 0)),
        thinking_tokens=_int(getattr(raw, "thoughts_token_count", 0)),
    )


def _reason(value: Any) -> str:
    """Finish reasons arrive as enums; compare on the name, not the object."""
    return str(getattr(value, "name", value) or "")


def _raise_for_block(response: Any) -> None:
    """Inspect refusal signals before touching content.

    A blocked response has no usable text and may have no candidates at all, so
    reading `.text` first raises something unrelated and buries the real cause.
    """
    feedback = getattr(response, "prompt_feedback", None)
    blocked = _reason(getattr(feedback, "block_reason", None)) if feedback else ""
    if blocked and blocked != "BLOCKED_REASON_UNSPECIFIED":
        raise BlockedError(f"Prompt blocked (reason={blocked})")

    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise LlmError("Response contained no candidates")

    finish = _reason(getattr(candidates[0], "finish_reason", None))
    if finish in BLOCKING_FINISH_REASONS:
        raise BlockedError(f"Response blocked (finish_reason={finish})")
    if finish == "MAX_TOKENS":
        raise TruncatedError("Hit max_output_tokens; JSON is incomplete")
    if finish == "MALFORMED_FUNCTION_CALL":
        raise LlmError("Model emitted a malformed function call")


def call_with_retry[R](
    call: Callable[[], R],
    *,
    what: str,
    attempts: int = 4,
    base_delay: float = 2.0,
) -> R:
    """Retry a provider call while the failure looks transient.

    Takes a thunk rather than the call's arguments so the embeddings endpoint
    can share this. The judgement about *which* failures are worth retrying --
    transient status codes, minus the per-day quota wall -- is the part worth
    having in one place; the shape of the request is not.
    """
    last: BaseException | None = None

    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            if not _is_transient(exc):
                raise
            last = exc
            if attempt < attempts - 1:
                time.sleep(_server_retry_delay(exc) or base_delay * (2**attempt))

    raise LlmError(f"{what} unavailable after {attempts} attempts: {last}") from last


def _call_with_retry(
    client: GenaiLike,
    *,
    model: str,
    contents: Any,
    config: Any,
    attempts: int = 4,
    base_delay: float = 2.0,
) -> Any:
    return call_with_retry(
        lambda: client.models.generate_content(model=model, contents=contents, config=config),
        what=model,
        attempts=attempts,
        base_delay=base_delay,
    )


def _function_calls(response: Any) -> list[Any]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    return [part.function_call for part in parts if getattr(part, "function_call", None)]


ToolDispatch = Callable[[str, dict[str, Any]], dict[str, Any]]
"""Runs one tool call: `(name, arguments) -> function response payload`."""

MAX_TOOL_TURNS = 3
"""How many times the model may call a tool before it has to answer.

An unbounded loop is a cost bug waiting to happen, and a model that keeps
searching is not converging. On the final turn the tools are withdrawn from the
request entirely, which is what makes termination a property of the code rather
than a hope about the model's behaviour.
"""


def _record(model: str, usage: Usage) -> None:
    # Reported to whatever span encloses this call, if any. A no-op outside a
    # trace, so the eval harness and unit tests need no observability wiring.
    record_llm_usage(
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cached_input_tokens,
        thinking_tokens=usage.thinking_tokens,
    )


def structured_call[T: BaseModel](
    client: GenaiLike,
    *,
    model: str,
    system: str,
    user: str,
    schema: type[T],
    thinking_level: str | None = None,
    max_output_tokens: int = 4096,
    tools: list[dict[str, Any]] | None = None,
    dispatch: ToolDispatch | None = None,
    max_tool_turns: int = MAX_TOOL_TURNS,
) -> Completion[T]:
    """Ask for JSON matching `schema`, optionally letting the model use tools.

    `thinking_level` is one of MINIMAL / LOW / MEDIUM / HIGH; `None` leaves the
    model's default alone. Note this is the Gemini **3.x** knob -- the 2.x
    `thinking_budget` integer is rejected with a bare
    `400 Request contains an invalid argument` on 3.x models, which names
    nothing and is thoroughly unhelpful to debug.

    Passing `tools` keeps `response_json_schema` in force: the model may call a
    function, and the turn *after* the results come back still returns
    schema-valid JSON. Verified against the API rather than assumed -- the two
    features are commonly believed to be mutually exclusive, and if they were,
    a searching extractor would have had to give up validated output.

    Without tools the request is byte-identical to what it was before this
    parameter existed, down to `contents` being a bare string rather than a list.
    The frozen extraction baseline was measured with that exact shape and should
    not move because an unrelated feature was added.
    """
    from google.genai import types

    if tools and dispatch is None:
        raise ValueError("tools were supplied with no dispatch to run them")

    declarations = (
        [types.Tool(function_declarations=[types.FunctionDeclaration(**t) for t in tools])]
        if tools
        else None
    )

    def _config(with_tools: bool) -> Any:
        return types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=response_json_schema(schema),
            max_output_tokens=max_output_tokens,
            tools=declarations if with_tools else None,
            thinking_config=(
                types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking_level))
                if thinking_level is not None
                else None
            ),
        )

    if not declarations:
        response = _call_with_retry(client, model=model, contents=user, config=_config(False))
        usage = _usage(response)
        _record(model, usage)
        return Completion(parsed=_parse(response, schema), usage=usage, model=model)

    history: list[Any] = [types.Content(role="user", parts=[types.Part(text=user)])]
    total = Usage()

    for turn in range(max_tool_turns + 1):
        response = _call_with_retry(
            client, model=model, contents=history, config=_config(turn < max_tool_turns)
        )
        _raise_for_block(response)

        usage = _usage(response)
        _record(model, usage)
        total = _add(total, usage)

        calls = _function_calls(response)
        if not calls:
            return Completion(parsed=_parse(response, schema), usage=total, model=model)

        assert dispatch is not None
        history.append(response.candidates[0].content)
        history.append(
            types.Content(
                role="user",
                parts=[
                    types.Part.from_function_response(
                        name=call.name or "",
                        response=dispatch(call.name or "", dict(call.args or {})),
                    )
                    for call in calls
                ],
            )
        )

    raise LlmError(f"{model} kept calling tools after {max_tool_turns} turns")


def _add(left: Usage, right: Usage) -> Usage:
    return Usage(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
        thinking_tokens=left.thinking_tokens + right.thinking_tokens,
    )


def _parse[T: BaseModel](response: Any, schema: type[T]) -> T:
    _raise_for_block(response)

    text = getattr(response, "text", None)
    if not text:
        raise LlmError("Response contained no text")

    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        # Don't trust response.parsed blindly: the SDK populates it only on a
        # clean parse, and validating ourselves keeps the error message specific
        # about which field the model got wrong.
        raise LlmError(f"Response did not match {schema.__name__}: {exc}") from exc
