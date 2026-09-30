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
from typing import Literal, get_args

import psycopg

from app.graph.nodes import MAX_REVISIONS

Action = Literal["confirm", "edit", "cancel", "sweep"]
Via = Literal["web", "cli", "telegram", "sweep"]
DecisionStatus = Literal["queued", "stale", "not_found", "invalid"]

ACTIONS: frozenset[str] = frozenset(get_args(Action))
VIAS: frozenset[str] = frozenset(get_args(Via))
SWEEPERS = frozenset({"cli", "sweep"})
"""Who may sweep. A sweep is an operator ending observe mode, never an owner's
tap, and M24 counts the two differently."""

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
    """Validate, then claim the proposal and enqueue the decision atomically.

    The caller's connection must commit: autocommit, or `app.store.db.connect`,
    which commits on exit. On a connection already inside a transaction, the
    claim is only a savepoint until the caller commits.
    """
    # Typed, but the CLI and the API hand over strings.
    if action not in ACTIONS or via not in VIAS:
        return DecisionResult("invalid", detail="unknown action or channel")
    if action == "sweep" and via not in SWEEPERS:
        return DecisionResult("invalid", detail="only an operator sweeps")
    if via == "sweep" and action != "sweep":
        return DecisionResult("invalid", detail="the sweep path only sweeps")

    correction = (correction or "").strip()
    if "\x00" in correction:
        return DecisionResult("invalid", detail="the correction contains a NUL character")
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
            if (
                repeat := _same_open_decision(conn, message_id, action, revision, correction)
            ) is not None:
                # A retry after a lost response: answer as the first request was answered.
                return DecisionResult("queued", decision_id=repeat, detail="already queued")
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


def _same_open_decision(
    conn: psycopg.Connection, message_id: str, action: str, revision: int, correction: str
) -> int | None:
    """The open decision this request repeats, if any.

    Only an open one: once a decision has settled, the same request is stale.
    The card has moved on, and replaying it would act on a state the owner
    has not seen.
    """
    row = conn.execute(
        """
        SELECT id FROM decisions
         WHERE message_id = %s AND outcome IS NULL
           AND action = %s AND revision = %s AND coalesce(correction, '') = %s
        """,
        (message_id, action, revision, correction if action == "edit" else ""),
    ).fetchone()
    return None if row is None else int(row[0])
