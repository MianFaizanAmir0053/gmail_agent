"""The two keys M24 counts decisions by.

**Pipeline version.** Autonomy is earned by one pipeline's record, not
inherited by the next: approvals given to proposals from one prompt say little
about proposals from another. The version is a hash of everything that shapes
a proposal, so M24 can reset its counts when that changes, and only then.

**Action type.** A hold on the owner's own calendar and an invite that emails
other people are different risks (tiers T1 and T2), and are counted apart.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Literal

from app.config import Settings

ActionType = Literal["calendar_hold", "calendar_invite"]

LEGACY_PIPELINE_VERSION = "pre-m16"
"""For proposals parked before M16 recorded a version. M24 excludes them."""

PIPELINE_REVISION = 3
"""Bump by hand for a change the hash cannot see, such as the code that lays
out an email for the model (`app/extraction/prompts.py`, `user_content`).

2: the owner's aliases are stripped from guests as well as `OWNER_EMAIL`
(M17), so a proposal whose only guest was an alias is now a hold.

3: M18. The email sits between per-call markers, scrubbed again at assembly;
the owner's correction moved to the system instruction; the body is the HTML
the owner sees, with hidden content removed; no reader holds a tool."""


def pipeline_version(settings: Settings) -> str:
    """Twelve hex characters naming what shaped a proposal: each call's model,
    prompt and schema. A setting that cannot affect a proposal must not reset
    M24's evidence, so nothing else counts.
    """
    from app.extraction import payloads, prompts

    parts: dict[str, Any] = {
        "revision": PIPELINE_REVISION,
        "classify": {
            "model": settings.classify_model,
            "system": prompts.CLASSIFY_SYSTEM,
            "question": prompts.MEETING_QUESTION,
            "criteria": prompts.MEETING_CRITERIA,
            "schema": payloads.ClassifyPayload.model_json_schema(),
        },
        "extract": {
            "model": settings.extraction_model,
            "system": prompts.EXTRACT_SYSTEM,
            "schema": payloads.ExtractionPayload.model_json_schema(),
        },
    }
    canonical = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def action_type(attendees: Sequence[str]) -> ActionType:
    """Guests make it an invite. The owner is never among the attendees:
    extraction removes their address (`app/extraction/payloads.py`)."""
    return "calendar_invite" if attendees else "calendar_hold"
