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
    output_tokens: int = 0
    cached_input_tokens: int = 0
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


def _call_with_retry(
    client: GenaiLike,
    *,
    model: str,
    user: str,
    config: Any,
    attempts: int = 4,
    base_delay: float = 2.0,
) -> Any:
    last: BaseException | None = None

    for attempt in range(attempts):
        try:
            return client.models.generate_content(model=model, contents=user, config=config)
        except Exception as exc:
            if not _is_transient(exc):
                raise
            last = exc
            if attempt < attempts - 1:
                time.sleep(_server_retry_delay(exc) or base_delay * (2**attempt))

    raise LlmError(f"{model} unavailable after {attempts} attempts: {last}") from last


def structured_call[T: BaseModel](
    client: GenaiLike,
    *,
    model: str,
    system: str,
    user: str,
    schema: type[T],
    thinking_level: str | None = None,
    max_output_tokens: int = 4096,
) -> Completion[T]:
    """Ask for JSON matching `schema`.

    `thinking_level` is one of MINIMAL / LOW / MEDIUM / HIGH; `None` leaves the
    model's default alone. Note this is the Gemini **3.x** knob -- the 2.x
    `thinking_budget` integer is rejected with a bare
    `400 Request contains an invalid argument` on 3.x models, which names
    nothing and is thoroughly unhelpful to debug.
    """
    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=response_json_schema(schema),
        max_output_tokens=max_output_tokens,
        thinking_config=(
            types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking_level))
            if thinking_level is not None
            else None
        ),
    )

    response = _call_with_retry(client, model=model, user=user, config=config)

    _raise_for_block(response)

    text = getattr(response, "text", None)
    if not text:
        raise LlmError("Response contained no text")

    try:
        parsed = schema.model_validate_json(text)
    except ValidationError as exc:
        # Don't trust response.parsed blindly: the SDK populates it only on a
        # clean parse, and validating ourselves keeps the error message specific
        # about which field the model got wrong.
        raise LlmError(f"Response did not match {schema.__name__}: {exc}") from exc

    usage = _usage(response)

    # Reported to whatever span encloses this call, if any. A no-op outside a
    # trace, so the eval harness and unit tests need no observability wiring.
    record_llm_usage(
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cached_input_tokens,
        thinking_tokens=usage.thinking_tokens,
    )

    return Completion(parsed=parsed, usage=usage, model=model)
