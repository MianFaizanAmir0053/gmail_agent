"""Recording a decision (M16, D1).

Every channel -- the web app, the CLI, Telegram and the sweep -- records a
decision here and returns. Nothing here resumes a thread: the worker
(`app/channel/worker.py`) is the only thing that does, so a recovery path can
never race a resume that is still running.

The claim is a conditional `UPDATE` on the proposal, `pending` at the revision
the owner saw. Two taps on one card cannot both succeed: the second waits for
the first's row lock, then finds the proposal no longer `pending`.

**A Confirm also binds what the owner saw (M17, D2).** It carries a token --
the first 12 characters of the proposal's keyed hash, the mode shown and the
proposal's generation -- and the claim holds it to all three, and to the
current `DRY_RUN`. In the same transaction it records the approval the
registry will check at execution: an `outbound_actions` row with a nonce.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Literal, get_args

import psycopg

from app.graph.nodes import MAX_REVISIONS
from app.policy import audit
from app.policy.hashing import INVITE

Action = Literal["confirm", "edit", "cancel", "sweep"]
Via = Literal["web", "cli", "telegram", "sweep"]
DecisionStatus = Literal["queued", "stale", "not_found", "invalid", "not_ready"]

ACTIONS: frozenset[str] = frozenset(get_args(Action))
VIAS: frozenset[str] = frozenset(get_args(Via))
SWEEPERS = frozenset({"cli", "sweep"})
"""Who may sweep. A sweep is an operator ending observe mode, never an owner's
tap, and M24 counts the two differently."""

MAX_CORRECTION_CHARS = 2000
"""Enough for any instruction a person types on a phone. The correction goes
into a model prompt, so it is bounded like any other input."""

_TOKEN = re.compile(r"(?P<prefix>[0-9a-f]{12})-(?P<mode>dry|live)-(?P<generation>[1-9][0-9]{0,8})")


def card_token(args_hash: str, dry_run: bool, generation: int) -> str:
    """What a card carries for its Confirm, such as `3f9a1c07be42-live-2`.

    The web app builds the same string (`dashboard/src/lib/timeline.ts`); a
    test on each side pins the format.
    """
    return f"{args_hash[:12]}-{'dry' if dry_run else 'live'}-{generation}"


def parse_token(token: str | None) -> tuple[str, bool, int] | None:
    """`(hash prefix, dry_run, generation)`, or None for anything malformed."""
    match = _TOKEN.fullmatch(token or "")
    if match is None:
        return None
    return match["prefix"], match["mode"] == "dry", int(match["generation"])


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
    token: str | None = None,
    dry_run: bool | None = None,
) -> DecisionResult:
    """Validate, then claim the proposal and enqueue the decision atomically.

    A Confirm needs `token`, from the card the owner tapped, and `dry_run`,
    the setting this process runs under. Other actions use neither.

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

    if action == "confirm":
        if dry_run is None:
            return DecisionResult("invalid", detail="the current mode is unknown")
        seen = parse_token(token)
        if seen is None:
            # A card or button from before M17, or a mangled one: it binds
            # nothing, so it confirms nothing.
            return DecisionResult("stale", detail="the card is out of date")
        return _confirm(conn, message_id, revision, via, seen, dry_run)

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


def _confirm(
    conn: psycopg.Connection,
    message_id: str,
    revision: int,
    via: Via,
    seen: tuple[str, bool, int],
    dry_run: bool,
) -> DecisionResult:
    """Claim the proposal only as the owner saw it, under the setting it was
    made in; then record the decision and the approval it binds, together."""
    prefix, seen_dry_run, generation = seen
    with conn.transaction():
        claimed = conn.execute(
            """
            UPDATE proposals
               SET status = 'deciding', updated_at = now()
             WHERE message_id = %s AND status = 'pending' AND revision = %s
               AND args_hash IS NOT NULL AND left(args_hash, 12) = %s
               AND dry_run = %s AND dry_run = %s AND generation = %s
            RETURNING action_type, pipeline_version,
                      GREATEST(EXTRACT(EPOCH FROM now() - parked_at), 0),
                      tool, args_hash
            """,
            (message_id, revision, prefix, seen_dry_run, dry_run, generation),
        ).fetchone()

        if claimed is None:
            return _why_not(conn, message_id, revision, dry_run, seen)

        action_type, pipeline_version, latency, tool, args_hash = claimed
        inserted = conn.execute(
            """
            INSERT INTO decisions
                   (message_id, revision, action, via, action_type, pipeline_version,
                    correction, decided_at, latency_seconds)
            VALUES (%s, %s, 'confirm', %s, %s, %s, NULL, now(), %s)
            RETURNING id
            """,
            (message_id, revision, via, action_type, pipeline_version, float(latency)),
        ).fetchone()
        assert inserted is not None
        decision_id = int(inserted[0])
        tier = 2 if tool == INVITE else 1
        conn.execute(
            """
            INSERT INTO outbound_actions
                   (decision_id, message_id, tool, tier, args_hash, dry_run, nonce)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (decision_id, message_id, tool, tier, args_hash, dry_run, secrets.token_hex(16)),
        )
        audit.record(
            conn,
            "action_approved",
            tool=tool,
            tier=tier,
            args_hash=args_hash,
            dry_run=dry_run,
            outcome="approved",
            decision_id=decision_id,
            message_id=message_id,
        )

    return DecisionResult("queued", decision_id=decision_id)


def _why_not(
    conn: psycopg.Connection,
    message_id: str,
    revision: int,
    dry_run: bool,
    seen: tuple[str, bool, int],
) -> DecisionResult:
    """Why a Confirm's claim found nothing, in the caller's words."""
    current = conn.execute(
        "SELECT revision, status, args_hash, dry_run FROM proposals WHERE message_id = %s",
        (message_id,),
    ).fetchone()
    if current is None:
        return DecisionResult("not_found", detail="no proposal for this message")
    row_revision, status, args_hash, row_dry_run = current
    if (repeat := _same_open_confirm(conn, message_id, revision, seen)) is not None:
        # A retry after a lost response: answer as the first request was answered.
        return DecisionResult("queued", decision_id=repeat, detail="already queued")
    if status == "pending" and row_revision == revision and args_hash is None:
        return DecisionResult("not_ready", detail="the proposal is being prepared")
    if status == "pending" and row_dry_run != dry_run:
        return DecisionResult(
            "stale", detail="made under another mode", current_revision=row_revision
        )
    return DecisionResult(
        "stale", detail=f"the proposal is {status}", current_revision=row_revision
    )


def _same_open_confirm(
    conn: psycopg.Connection, message_id: str, revision: int, seen: tuple[str, bool, int]
) -> int | None:
    """The open Confirm this request repeats: the same revision, and an
    approval bound to the same hash prefix and mode."""
    prefix, seen_dry_run, _ = seen
    row = conn.execute(
        """
        SELECT d.id FROM decisions d JOIN outbound_actions a ON a.decision_id = d.id
         WHERE d.message_id = %s AND d.outcome IS NULL AND d.action = 'confirm'
           AND d.revision = %s AND left(a.args_hash, 12) = %s AND a.dry_run = %s
        """,
        (message_id, revision, prefix, seen_dry_run),
    ).fetchone()
    return None if row is None else int(row[0])


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
