"""The extractor plus the reviewer, as one scorable thing.

Satisfies `app.eval.dataset.Extractor`, so the harness scores the pair exactly
as it scores the extractor alone and the delta is like-for-like.

The revision cap is duplicated here rather than imported from the graph, and
that is a real cost. The alternative is worse: running the eval through
LangGraph would drag in a checkpointer, a ledger, and a Postgres connection, and
the harness would stop being runnable on a laptop with no database. The number
that matters is whether reviewing changes the extraction, and that question does
not need a graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.agents.reviewer import Reviewer
from app.contracts import EmailMessage, ExtractionResult
from app.extraction.pipeline import ExtractionPipeline
from app.graph.nodes import MAX_REVIEW_ROUNDS


@dataclass(slots=True)
class ReviewedExtractor:
    pipeline: ExtractionPipeline
    reviewer: Reviewer

    def __call__(
        self, email: EmailMessage, *, now_utc: datetime, user_timezone: str
    ) -> ExtractionResult:
        triage = self.pipeline.classify(email, now_utc=now_utc, user_timezone=user_timezone)
        if not triage.is_meeting:
            return ExtractionResult(
                is_meeting=False, confidence=triage.confidence, reasoning=triage.reasoning
            )

        extraction = self.pipeline.extract(email, now_utc=now_utc, user_timezone=user_timezone)
        feedback = ""

        for _ in range(MAX_REVIEW_ROUNDS + 1):
            verdict = self.reviewer(email, extraction, now_utc=now_utc, user_timezone=user_timezone)

            if verdict.decision == "approve":
                return extraction

            if verdict.decision == "reject":
                # Scored as "not a meeting", which is what a rejection means to
                # everything downstream. The reasoning keeps the reviewer's
                # objection visible in the results file.
                objection = "; ".join(verdict.issues) or verdict.reasoning
                return ExtractionResult(
                    is_meeting=False,
                    confidence=verdict.confidence,
                    reasoning=f"Reviewer rejected: {objection}",
                )

            feedback = (
                f"A reviewer found these problems with your previous answer:\n{verdict.feedback()}"
            )
            extraction = self.pipeline.extract(
                email, now_utc=now_utc, user_timezone=user_timezone, extra=feedback
            )

        return extraction


def build_reviewed(owner_email: str = "me@example.com") -> ReviewedExtractor:
    """Wire the pair from settings.

    The reviewer gets `search_context` when a database is reachable and goes
    without when it is not, rather than refusing to run. Which tools it actually
    had is reported by the runner, because a reviewer with no independent
    evidence is measuring something different from one with it.
    """
    from app.agents.reviewer import build_reviewer
    from app.extraction.pipeline import build_pipeline

    return ReviewedExtractor(
        pipeline=build_pipeline(owner_email=owner_email),
        reviewer=build_reviewer(),
    )
