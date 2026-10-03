"""Shared data contracts.

Every module reads or writes these types. Stable boundaries here are what let
modules be replaced without touching their neighbours:

    M02 eval harness  scores  ExtractionResult
    M05 graph         passes  EmailMessage -> ExtractionResult -> ActionResult
    M09 dashboard     renders all three

Change these deliberately, not casually.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class EmailMessage(BaseModel):
    """A single Gmail message, normalised."""

    id: str
    """Gmail message ID. Also the idempotency key (M04) and graph thread_id (M05)."""

    thread_id: str
    subject: str
    body_text: str
    sender: str
    recipients: list[str] = Field(default_factory=list)
    received_at: datetime
    credential: bool = False
    """It carried a sign-in code, a sign-in or reset link, or a secret (M18,
    decision 2). Its body is then `app.policy.scrub.CREDENTIAL_NOTICE`, and
    the graph sets it aside before any model reads it. Checkpoints made before
    M18 lack the field, and read as False."""


class ExtractionResult(BaseModel):
    """What the model believes about a message.

    Times are stored twice on purpose: ``start_utc``/``end_utc`` are what the
    Calendar API needs and what M02 scores, while ``timezone`` preserves the
    originating IANA zone so a proposal can be rendered back to a human in the
    zone they actually think in.
    """

    is_meeting: bool
    title: str | None = None
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    timezone: str | None = None
    """IANA zone name, e.g. "Asia/Karachi". Not an offset -- offsets lose DST."""

    attendees: list[str] = Field(default_factory=list)
    location: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class ActionResult(BaseModel):
    """Outcome of attempting to act on an ExtractionResult.

    `dry_run` is its own status rather than folded into `skipped_duplicate`.
    They mean different things -- "the flag was on" versus "we already booked
    this" -- and a dashboard that cannot tell them apart hides the fact that a
    deployment has been writing nothing all week.
    """

    status: Literal["created", "skipped_duplicate", "rejected", "failed", "dry_run"]
    event_id: str | None = None
    error: str | None = None
