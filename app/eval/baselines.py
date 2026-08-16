"""Trivial extractors that any real one must beat.

`always_no` matters more than it looks. Most email is not a meeting, so an
extractor that never says "meeting" scores surprisingly well on accuracy while
being completely useless. It is the majority-class floor: quoting a headline
number without comparing against it is how people accidentally report a model
as good when it has learned nothing.
"""

from __future__ import annotations

from datetime import datetime

from app.contracts import EmailMessage, ExtractionResult


def always_no(email: EmailMessage, *, now_utc: datetime, user_timezone: str) -> ExtractionResult:
    """Majority-class baseline: nothing is ever a meeting."""
    return ExtractionResult(
        is_meeting=False,
        confidence=0.0,
        reasoning="baseline: always_no",
    )


def always_yes(email: EmailMessage, *, now_utc: datetime, user_timezone: str) -> ExtractionResult:
    """Recall-1.0 baseline: everything is a meeting, no details.

    Its precision is the corpus base rate, and it gets every event field wrong,
    so exact-match lands at zero. Useful as the other bookend.
    """
    return ExtractionResult(
        is_meeting=True,
        confidence=0.0,
        reasoning="baseline: always_yes",
    )
