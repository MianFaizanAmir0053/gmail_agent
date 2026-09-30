"""Recording a decision (M16, D1).

Every channel -- the web app, the CLI, Telegram and the sweep -- records a
decision here and returns. Nothing here resumes a thread: the worker
(`app/channel/worker.py`) is the only thing that does, so a recovery path can
never race a resume that is still running.

The claim is a conditional `UPDATE` on the proposal, `pending` at the revision
the owner saw. Two taps on one card cannot both succeed: the second waits for
the first's row lock, then finds the proposal no longer `pending`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import psycopg

from app.graph.nodes import MAX_REVISIONS

Action = Literal["confirm", "edit", "cancel", "sweep"]
Via = Literal["web", "cli", "telegram", "sweep"]
DecisionStatus = Literal["queued", "stale", "not_found", "invalid"]

MAX_CORRECTION_CHARS = 2000
"""Enough for any instruction a person types on a phone. The correction goes
into a model prompt, so it is bounded like any other input."""


@dataclass(frozen=True, slots=True)
class DecisionResult:
    status: DecisionStatus
    decision_id: int | None = None
    detail: str = ""
    current_revision: int | None = None
    """For a stale card: the revision now pending, so the caller can show it."""


def decide(
    conn: psycopg.Connection,
    message_id: str,
    *,
    action: Action,
    revision: int,
    correction: str = "",
    via: Via,
) -> DecisionResult:
    """Validate, then claim the proposal and enqueue the decision atomically."""
    correction = correction.strip()
    if action == "edit":
        # An empty correction would reach `reject` and be logged as "declined
        # by user"; an edit past the cap would be rejected silently
        # (`app/graph/build.py`, `_decision`). Refuse both here instead.
        if not correction:
            return DecisionResult("invalid", detail="an edit needs a correction")
        if revision > MAX_REVISIONS:
            return DecisionResult("invalid", detail="no edits left at this revision")
        if len(correction) > MAX_CORRECTION_CHARS:
            return DecisionResult("invalid", detail="the correction is too long")

    with conn.transaction():
        claimed = conn.execute(
            """
            UPDATE proposals
               SET status = 'deciding', updated_at = now()
             WHERE message_id = %s AND status = 'pending' AND revision = %s
            RETURNING action_type, pipeline_version,
                      GREATEST(EXTRACT(EPOCH FROM now() - parked_at), 0)
            """,
            (message_id, revision),
        ).fetchone()

        if claimed is None:
            current = conn.execute(
                "SELECT revision, status FROM proposals WHERE message_id = %s", (message_id,)
            ).fetchone()
            if current is None:
                return DecisionResult("not_found", detail="no proposal for this message")
            return DecisionResult(
                "stale", detail=f"the proposal is {current[1]}", current_revision=current[0]
            )

        action_type, pipeline_version, latency = claimed
        inserted = conn.execute(
            """
            INSERT INTO decisions
                   (message_id, revision, action, via, action_type, pipeline_version,
                    correction, decided_at, latency_seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s)
            RETURNING id
            """,
            (
                message_id,
                revision,
                action,
                via,
                action_type,
                pipeline_version,
                correction if action == "edit" else None,
                float(latency),
            ),
        ).fetchone()

    assert inserted is not None
    return DecisionResult("queued", decision_id=int(inserted[0]))
