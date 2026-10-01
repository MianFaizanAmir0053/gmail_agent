"""Classify, then extract.

Two calls rather than one. Most email is not a meeting, and running the full
extraction over every newsletter is the largest avoidable cost in the system --
the classify prompt is smaller and runs with thinking switched off entirely.

The pipeline satisfies `app.eval.dataset.Extractor`, so the eval harness scores
it exactly like a baseline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from app.contracts import EmailMessage, ExtractionResult
from app.extraction import prompts
from app.extraction.evaluation import (
    EVALUATION_MODELS,
    Evaluator,
    classify_by_evaluation,
)
from app.extraction.llm import GenaiLike, LlmError, Usage, structured_call
from app.extraction.payloads import (
    ClassifyPayload,
    ExtractionPayload,
    InvalidPayloadError,
    to_extraction_result,
)
from app.policy.budget import Gate
from app.tools.search_context import (
    SEARCH_CONTEXT_TOOL,
    TOOL_NAME,
    Searcher,
    execute_search_context,
)

MINIMAL_THINKING = "MINIMAL"
"""Triage is a yes/no call on text sitting right in front of the model.
Paying for reasoning tokens on "is this a job alert" is the easiest saving here."""


@dataclass(slots=True)
class RunStats:
    """Per-run counters. M08 replaces this with real trace rows."""

    classify_calls: int = 0
    extract_calls: int = 0
    search_calls: int = 0
    cache_hits: int = 0
    usages: list[Usage] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(u.input_tokens for u in self.usages)

    @property
    def output_tokens(self) -> int:
        return sum(u.output_tokens for u in self.usages)

    @property
    def thinking_tokens(self) -> int:
        return sum(u.thinking_tokens for u in self.usages)

    def record(self, usage: Usage) -> None:
        self.usages.append(usage)
        if usage.cache_hit:
            self.cache_hits += 1


def _rejected(reason: str, *, confidence: float = 0.0) -> ExtractionResult:
    return ExtractionResult(is_meeting=False, confidence=confidence, reasoning=reason)


@dataclass(slots=True)
class ExtractionPipeline:
    client: GenaiLike
    classify_model: str
    extraction_model: str
    owner_email: str = ""
    owner_aliases: tuple[str, ...] = ()
    """The owner's other addresses, stripped from guests like `owner_email`."""
    classify_thinking_level: str | None = MINIMAL_THINKING
    searcher: Searcher | None = None
    """Optional retrieval over past threads (M11).

    Left unset the pipeline behaves exactly as it did when the baseline was
    frozen -- no tool declaration, no extra system text, no extra turns. That is
    what makes "before retrieval" and "after retrieval" comparable numbers
    rather than two different systems.
    """
    evaluator: Evaluator | None = None
    """Answers `classify` when `classify_model` is an evaluation model served by
    Vercel AI Gateway, such as `typesafe-ai/jev`. Unused for a Gemini model."""
    stats: RunStats = field(default_factory=RunStats)

    def classify(
        self, email: EmailMessage, *, now_utc: datetime, user_timezone: str, extra: str = ""
    ) -> ClassifyPayload:
        """Cheap triage. Exposed separately so M05's graph can make it its own
        node, which M08 then gets per-stage timings and costs for."""
        user = self._user(email, now_utc, user_timezone, extra)
        if self.classify_model in EVALUATION_MODELS:
            if self.evaluator is None:
                raise RuntimeError(
                    f"{self.classify_model} runs on Vercel AI Gateway; set AI_GATEWAY_API_KEY"
                )
            verdict, usage = classify_by_evaluation(
                self.evaluator, model=self.classify_model, state=user
            )
        else:
            triage = structured_call(
                self.client,
                model=self.classify_model,
                system=prompts.CLASSIFY_SYSTEM,
                user=user,
                schema=ClassifyPayload,
                thinking_level=self.classify_thinking_level,
                max_output_tokens=1024,
            )
            verdict, usage = triage.parsed, triage.usage
        self.stats.classify_calls += 1
        self.stats.record(usage)
        return verdict

    def extract(
        self, email: EmailMessage, *, now_utc: datetime, user_timezone: str, extra: str = ""
    ) -> ExtractionResult:
        """Full extraction. `extra` carries a human correction on a re-run."""
        searching = self.searcher is not None
        detail = structured_call(
            self.client,
            model=self.extraction_model,
            system=(
                f"{prompts.EXTRACT_SYSTEM}\n{prompts.SEARCH_SUFFIX}"
                if searching
                else prompts.EXTRACT_SYSTEM
            ),
            user=self._user(email, now_utc, user_timezone, extra),
            schema=ExtractionPayload,
            max_output_tokens=4096,
            tools=[SEARCH_CONTEXT_TOOL] if searching else None,
            dispatch=self._dispatch if searching else None,
        )
        self.stats.extract_calls += 1
        self.stats.record(detail.usage)

        try:
            return to_extraction_result(
                detail.parsed, owner_email=self.owner_email, owner_aliases=self.owner_aliases
            )
        except InvalidPayloadError as exc:
            # A malformed event is not a calendar entry. Degrade to "not a
            # meeting" with the reason recorded, rather than proposing something
            # with a null start time that fails later and further from the cause.
            return _rejected(f"Discarded malformed extraction: {exc}")

    def __call__(
        self, email: EmailMessage, *, now_utc: datetime, user_timezone: str
    ) -> ExtractionResult:
        triage = self.classify(email, now_utc=now_utc, user_timezone=user_timezone)
        if not triage.is_meeting:
            return _rejected(triage.reasoning, confidence=triage.confidence)
        return self.extract(email, now_utc=now_utc, user_timezone=user_timezone)

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run a tool call the model asked for.

        An unknown name is answered rather than raised. The model can read the
        error and move on; aborting a whole extraction because it hallucinated a
        function name would be a far worse trade.
        """
        if name != TOOL_NAME or self.searcher is None:
            return {"error": f"No such tool: {name}", "results": []}
        self.stats.search_calls += 1
        return execute_search_context(self.searcher, args)

    def _user(self, email: EmailMessage, now_utc: datetime, user_timezone: str, extra: str) -> str:
        content = prompts.user_content(email, now_utc=now_utc, user_timezone=user_timezone)
        if extra:
            # Appended verbatim. The caller labels it, because by M13 there are
            # two possible sources -- a human and the reviewer agent -- and only
            # the caller knows which one this is.
            content = f"{content}\n{extra}\n"
        return content


def build_pipeline(
    owner_email: str = "",
    searcher: Searcher | None = None,
    *,
    owner_aliases: tuple[str, ...] = (),
    gate: Gate | None = None,
) -> ExtractionPipeline:
    """Wire a pipeline from settings, its calls metered by `gate` (M17, D5):
    the session's, or for a command-line tool one on a connection of its own.

    Imports the SDK lazily so the eval harness and unit tests never need an API
    key just to import this module.
    """
    from app.config import get_settings
    from app.policy import models

    settings = get_settings()
    meter = gate or models.local_gate(settings)
    return ExtractionPipeline(
        client=cast(GenaiLike, models.client(settings, meter)),
        classify_model=settings.classify_model,
        extraction_model=settings.extraction_model,
        owner_email=owner_email,
        owner_aliases=owner_aliases,
        searcher=searcher,
        evaluator=models.evaluator(settings, meter),
    )


__all__ = ["ExtractionPipeline", "LlmError", "RunStats", "build_pipeline"]
