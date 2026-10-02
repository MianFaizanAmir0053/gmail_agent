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
from app.policy import audit, contacts
from app.policy.participants import guest_key
from app.policy.registry import TOOLS

Action = Literal["confirm", "edit", "cancel", "sweep"]
Via = Literal["web", "cli", "telegram", "sweep"]
DecisionStatus = Literal["queued", "stale", "not_found", "invalid", "not_ready", "outside"]

ACTIONS: frozenset[str] = frozenset(get_args(Action))
VIAS: frozenset[str] = frozenset(get_args(Via))
SWEEPERS = frozenset({"cli", "sweep"})
"""Who may sweep. A sweep is an operator ending observe mode, never an owner's
tap, and M24 counts the two differently."""

OUTSIDE_GUESTS = "allow or remove the guests outside the thread first"
"""Why a Confirm on an invite is refused while a guest is outside (M17, D4)."""

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
    reason: str | None = None,
) -> DecisionResult:
    """Validate, then claim the proposal and enqueue the decision atomically.

    A Confirm needs `token`, from the card the owner tapped, and `dry_run`,
    the setting this process runs under. Other actions use neither. A sweep
    may give a `reason`, one of the audit log's fixed phrases; the worker
    hands it to the graph, and it becomes the ledger's.

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
    if reason is not None and (action != "sweep" or reason not in audit.REASONS.values()):
        return DecisionResult("invalid", detail="only a sweep gives a reason, and a fixed one")

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
                    correction, reason, decided_at, latency_seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
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
                reason,
                float(latency),
            ),
        ).fetchone()

    assert inserted is not None
    return DecisionResult("queued", decision_id=int(inserted[0]))


WithdrawStatus = Literal["requested", "settled", "not_found"]


def request_withdraw(conn: psycopg.Connection, decision_id: int) -> WithdrawStatus:
    """Ask for a queued decision to be withdrawn (M17, D6).

    Only the worker may settle a decision it might have applied (M16, D1),
    so this records the request and nothing more. The worker carries it out
    before anything else, or declines it if the decision is already being
    applied. Asking twice changes nothing. A sweep is the operator's, never a
    tap, and is not the owner's to withdraw: as far as Withdraw goes, it does
    not exist.
    """
    row = conn.execute(
        """
        UPDATE decisions SET withdraw_requested_at = coalesce(withdraw_requested_at, now())
         WHERE id = %s AND outcome IS NULL AND action <> 'sweep'
        RETURNING id
        """,
        (decision_id,),
    ).fetchone()
    if row is not None:
        return "requested"
    found = conn.execute(
        "SELECT 1 FROM decisions WHERE id = %s AND action <> 'sweep'", (decision_id,)
    ).fetchone()
    return "settled" if found is not None else "not_found"


def expire(conn: psycopg.Connection, message_id: str, revision: int) -> bool:
    """End a pending proposal made under another `DRY_RUN` (M17, D2). True
    when this call recorded the expiry.

    A sweep, so the thread ends REJECTED the way M15's sweep ends it, with
    "made under another mode" as the reason. Turning `DRY_RUN` off starts
    afresh: the proposal is never shown again as if it could run. The sweep
    and its audit row are one transaction; an expiry already queued is not
    recorded twice.
    """
    phrase = audit.REASONS["mode"]
    with conn.transaction():
        result = decide(
            conn, message_id, action="sweep", revision=revision, via="sweep", reason=phrase
        )
        if result.status != "queued" or result.detail == "already queued":
            return False
        audit.record(
            conn,
            "proposal_expired",
            decision_id=result.decision_id,
            message_id=message_id,
            reason=phrase,
        )
    return True


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
        if _unconfirmed_outsiders(conn, message_id, revision, seen, dry_run):
            # Nothing is recorded: the owner allows or removes them, then
            # confirms again (D4).
            return DecisionResult("outside", detail=OUTSIDE_GUESTS)
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
        tier = int(TOOLS[tool])
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


def _unconfirmed_outsiders(
    conn: psycopg.Connection,
    message_id: str,
    revision: int,
    seen: tuple[str, bool, int],
    dry_run: bool,
) -> list[str]:
    """The card's outside guests the owner has not allowed, for the proposal
    exactly as the card showed it. Locked for the claim that follows. Any
    other card is answered as stale by the claim, not told about guests it
    does not show."""
    prefix, seen_dry_run, generation = seen
    row = conn.execute(
        """
        SELECT payload->'outside_guests' FROM proposals
         WHERE message_id = %s AND status = 'pending' AND revision = %s
           AND args_hash IS NOT NULL AND left(args_hash, 12) = %s
           AND dry_run = %s AND dry_run = %s AND generation = %s
           FOR UPDATE
        """,
        (message_id, revision, prefix, seen_dry_run, dry_run, generation),
    ).fetchone()
    guests = [str(guest) for guest in ((row[0] if row else None) or [])]
    if not guests:
        return []
    allowed = contacts.confirmed(conn, guests)
    return [guest for guest in guests if guest_key(guest) not in allowed]


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
