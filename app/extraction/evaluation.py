"""Triage on an evaluation model, through Vercel AI Gateway.

Evaluation models answer typed questions about a piece of state with
probabilities instead of prose. "Should this email create a calendar event?" is
exactly a boolean question, and TypeSafe's `typesafe-ai/jev` bills input only --
so it can stand in for the Gemini classify call at a fraction of the price.

Plain HTTP against one endpoint, for the same reason as the Telegram client. The
gateway serves evaluation only at `/v1/evaluate`, not through its
OpenAI-compatible routes, and the only SDK that speaks it is the TypeScript one.

**Unmeasured.** Nothing here has been scored against the golden set. Gemini
stays the default classifier until `eval --extractor jev` says otherwise.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.extraction import prompts
from app.extraction.llm import LlmError, Usage, call_with_retry
from app.extraction.payloads import ClassifyPayload
from app.obs.trace import record_llm_usage

JEV = "typesafe-ai/jev"

EVALUATION_MODELS = frozenset({JEV})
"""Classify models answered by the gateway's evaluate endpoint, not by Gemini."""

MEETING_THRESHOLD = 0.5
"""P(calendar event) at or above which an email goes on to extraction.

A neutral starting point, not a tuned one. A miss here loses a meeting silently,
while a false positive only costs an extraction call that can still say no --
so the golden set, not intuition, should be what moves it.
"""

STATE_CHAR_LIMIT = 40_000
"""Jev reads at most 32,000 tokens of state plus question. Forty thousand
characters stays under that even for scripts that tokenise at under two
characters a token; a longer email is cut, and its verdict says so."""

_QUESTION_ID = "calendar_event"


class GatewayError(RuntimeError):
    """AI Gateway answered with an error status.

    Carries `status_code` so `call_with_retry` can tell a 429 or 503 worth
    retrying from a 400 that never improves.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"AI Gateway {status_code}: {message}")
        self.status_code = status_code


class Evaluator(Protocol):
    """Just enough surface to allow a fake in tests. The Gateway's own is built
    by `app/policy/models.py`, the metered path (M17, D5)."""

    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]: ...


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage(body: dict[str, Any]) -> Usage:
    raw = body.get("usage")
    if not isinstance(raw, dict):
        return Usage()
    return Usage(
        input_tokens=_count(raw.get("inputTokens")), output_tokens=_count(raw.get("outputTokens"))
    )


def _probability(body: dict[str, Any]) -> float:
    answers = body.get("answers")
    answer = answers.get(_QUESTION_ID) if isinstance(answers, dict) else None
    value = answer.get("probability") if isinstance(answer, dict) else None
    # bool is an int subclass, so True would otherwise read as certainty.
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise LlmError(f"Evaluation returned no usable probability: {answer!r}")
    return float(value)


def classify_by_evaluation(
    evaluator: Evaluator,
    *,
    model: str,
    state: str,
    base_delay: float = 2.0,
) -> tuple[ClassifyPayload, Usage]:
    """Ask `model` whether the email in `state` should create a calendar event.

    `state` is the same text the Gemini classifier reads -- grounding block plus
    email -- so an eval difference between the two is the model's, not the
    prompt's.
    """
    cut = len(state) > STATE_CHAR_LIMIT
    request = {
        "model": model,
        "state": state[:STATE_CHAR_LIMIT],
        "questions": {
            _QUESTION_ID: {
                "type": "boolean",
                "instructions": prompts.MEETING_QUESTION,
                "criteria": prompts.MEETING_CRITERIA,
            }
        },
    }
    body = call_with_retry(lambda: evaluator.evaluate(request), what=model, base_delay=base_delay)

    # Recorded before the answer is checked: the tokens were billed either way.
    usage = _usage(body)
    ran = str(body.get("model") or model)
    record_llm_usage(
        model=ran,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=0,
        thinking_tokens=0,
    )

    probability = _probability(body)
    is_meeting = probability >= MEETING_THRESHOLD
    note = f"; the email was cut to its first {STATE_CHAR_LIMIT:,} characters" if cut else ""
    verdict = ClassifyPayload(
        is_meeting=is_meeting,
        confidence=probability if is_meeting else 1.0 - probability,
        reasoning=f"{ran} put P(calendar event) at {probability:.2f}{note}.",
    )
    return verdict, usage
