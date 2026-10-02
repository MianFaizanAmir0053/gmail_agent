"""The registry (M17, D1-D3).

Every action with a side effect is a registered tool, and this is the only
code that runs one. A tool's tier says what it needs:

| Tier     | Needs                                                        |
|----------|--------------------------------------------------------------|
| READ     | nothing; runs where it is needed and is not audited          |
| INTERNAL | the owner's approval, bound to its exact arguments           |
| EXTERNAL | the same; D4's recipient rule joins it in 17.8               |

T3 has no member: an action that must never run is never registered.

**An approval is checked again here,** though `decide()` and the worker have
checked it already: the nonce, in constant time; the arguments' hash; and the
mode it was approved under. Every check runs before the action is marked
started. A refusal is recorded on the action and in the audit log, and `act`
marks the message FAILED with its reason.

**A write interrupted halfway is finished, never redone (D3).** The first
attempt stores the calendar, the event's own id and the exact request as the
action starts executing, and commits that before calling Google. A later
attempt replays what was stored: it asks Google for that id first, and
inserts the stored request only if Google has no such event. Under `DRY_RUN`
it only asks: the kill switch never writes.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Any, Literal, Protocol

import psycopg
from psycopg.types.json import Jsonb

from app.google.calendar import WriteRejectedError, event_body
from app.policy import audit, control
from app.policy.hashing import HOLD, INVITE, args_hash, event_id_for
from app.tools.calendar_tool import CreateEventInput


class Tier(IntEnum):
    READ = 0
    """T0: runs without approval."""
    INTERNAL = 1
    """T1: reversible and internal. One tap; eligible for M24."""
    EXTERNAL = 2
    """T2: leaves the owner's account. Approval every time."""


TOOLS: dict[str, Tier] = {
    "calendar.freebusy": Tier.READ,
    HOLD: Tier.INTERNAL,
    INVITE: Tier.EXTERNAL,
}
"""Every registered tool. `tests/test_registry.py` pins this set: registering
a new one is the owner's call. The conflict check runs the READ tool itself."""


class HeldError(RuntimeError):
    """A check that cannot be made now: Gmail could not be read for an
    invite's guests (D4). Nothing started; the worker holds the decision
    without counting an attempt, for as long as D4 allows."""


class PausedError(RuntimeError):
    """The owner has paused the agent (D6). Nothing new runs; the worker
    releases its lease without counting an attempt."""


@dataclass(frozen=True, slots=True)
class Approval:
    """What a Confirm carries to `act`: the action `decide()` recorded, and
    its nonce."""

    action_id: int
    nonce: str = field(repr=False)

    @classmethod
    def parse(cls, value: object) -> Approval | None:
        """From the graph state, where the worker's resume put it. Anything
        else is no approval at all."""
        if not isinstance(value, Mapping):
            return None
        action_id, nonce = value.get("action_id"), value.get("nonce")
        if not isinstance(action_id, int) or isinstance(action_id, bool):
            return None
        if not isinstance(nonce, str):
            return None
        return cls(action_id=action_id, nonce=nonce)

    def to_state(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "nonce": self.nonce}


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Literal["created", "dry_run", "refused"]
    event_id: str | None = None
    reason: str | None = None
    """One of `audit.REASONS`' phrases, for a refusal."""


class Writer(Protocol):
    """The calendar writes the registry runs: `CalendarClient` in production."""

    @property
    def dry_run(self) -> bool: ...

    @property
    def calendar_id(self) -> str: ...

    def insert(self, calendar_id: str, body: dict[str, Any], *, event_id: str) -> str | None: ...

    def find(self, calendar_id: str, event_id: str) -> str | None: ...


Outsiders = Callable[[str, Sequence[str]], list[str]]
"""The guests of a message's proposal who are neither in its thread nor
confirmed contacts, read now (`contacts.unconfirmed_outsiders`)."""

Status = Literal["approved", "executing", "done", "dry_run", "refused", "failed"]


@dataclass(frozen=True, slots=True)
class _Action:
    id: int
    decision_id: int
    message_id: str
    tool: str
    tier: int
    args_hash: str
    dry_run: bool
    nonce: str = field(repr=False)
    status: Status
    calendar_id: str | None
    event_id: str | None
    request: dict[str, Any] | None = field(repr=False)
    reason: str | None


_KINDS: dict[str, audit.Kind] = {
    "done": "action_executed",
    "dry_run": "action_executed",
    "refused": "action_refused",
    "failed": "action_failed",
}


def refuse_approved(conn: psycopg.Connection, decision_id: int, *, reason: str) -> None:
    """A decision settled without its action running: an action still
    `approved` is refused with it, and audited (D2). Runs inside the caller's
    transaction, with the settle, so no action is left `approved` once its
    decision has settled. `reason` is a key of `audit.REASONS`."""
    phrase = audit.REASONS[reason]
    row = conn.execute(
        """
        UPDATE outbound_actions
           SET status = 'refused', reason = %s, request = NULL, finished_at = now()
         WHERE decision_id = %s AND status = 'approved'
        RETURNING message_id, tool, tier, args_hash, dry_run
        """,
        (phrase, decision_id),
    ).fetchone()
    if row is None:
        return
    message_id, tool, tier, digest, dry_run = row
    audit.record(
        conn,
        "action_refused",
        tool=tool,
        tier=tier,
        args_hash=digest,
        dry_run=dry_run,
        outcome="refused",
        decision_id=decision_id,
        message_id=message_id,
        reason=phrase,
    )


class Registry:
    def __init__(
        self, conn: psycopg.Connection, writer: Writer, *, key: bytes, outsiders: Outsiders
    ) -> None:
        self._conn = conn
        self._writer = writer
        self._key = key
        self._outsiders = outsiders

    def execute(
        self,
        tool: str,
        args: CreateEventInput,
        *,
        approval: Approval | None,
        message_id: str,
    ) -> Outcome:
        """Run an approved calendar write, or finish one an earlier attempt
        began.

        Raises `PausedError` for a new action while the owner has paused the
        agent. Any other exception leaves the action where it was, for the
        next attempt to finish.
        """
        tier = TOOLS.get(tool)
        if tier is None or tier is Tier.READ:
            return self._refuse_unbound("unknown_tool", message_id=message_id)
        if approval is None:
            return self._refuse_unbound("nonce", message_id=message_id, tool=tool, tier=tier)

        # An invite's guests are read from Gmail before the row is locked, so
        # no lock is held across the network, and only for a new action: one
        # already under way never waits on Gmail to finish (D3).
        outsiders: list[str] | None = None
        if tier is Tier.EXTERNAL and self._status(approval.action_id) == "approved":
            if control.is_paused(self._conn):
                # Before Gmail: a pause holds the action at no cost, while a
                # Gmail outage may cost an attempt after an hour (D6).
                raise PausedError
            try:
                outsiders = self._outsiders(message_id, args.attendees)
            except Exception as exc:
                raise HeldError("the guests could not be checked") from exc

        with self._conn.transaction():
            action = self._load(approval.action_id, lock=True)
            if (
                action is None
                or action.message_id != message_id
                or not hmac.compare_digest(action.nonce, approval.nonce)
            ):
                # Not this message's approval. A row with that id, if there
                # is one, belongs to another decision and is left alone.
                return self._refuse_unbound("nonce", message_id=message_id, tool=tool, tier=tier)

            # This decision's own earlier attempt: its outcome stands, whatever
            # `tool` and `args` the current code builds (D3).
            if action.status == "done":
                return Outcome("created", event_id=action.event_id)
            if action.status == "dry_run":
                return Outcome("dry_run")
            if action.status in ("refused", "failed"):
                return Outcome("refused", reason=action.reason or audit.REASONS["used"])

            if action.status == "approved":
                if control.is_paused(self._conn):
                    raise PausedError
                refusal = self._check(action, tool, args, outsiders)
                if refusal is not None:
                    return self._refuse(action, refusal)
                if self._writer.dry_run:
                    self._end(action, "dry_run")
                    return Outcome("dry_run")
                action = self._start(action, args)
                first = True
            else:
                first = False
        # Committed: whatever happens to the call below, the next attempt
        # knows which event to look for, and where.
        return self._write(action, first=first)

    def look_up(self, action_id: int) -> str | None:
        """The event an action made, if it made one.

        For giving up (D3): an action whose attempts ran out may still have
        reached Google. A `done` action answers from what it stored; an
        `executing` one asks Google, and raises when Google cannot be asked,
        so that nothing is settled on a guess.
        """
        action = self._load(action_id, lock=False)
        if action is None:
            return None
        if action.status == "done":
            return action.event_id
        if action.status != "executing":
            return None
        assert action.calendar_id is not None and action.event_id is not None
        return self._writer.find(action.calendar_id, action.event_id)

    def close(self, action_id: int, *, event_id: str | None) -> None:
        """End an `executing` action the worker gave up on: `done` when its
        event was found, `failed` when it was never made. Runs inside the
        caller's transaction, with the decision's settle."""
        action = self._load(action_id, lock=True)
        if action is None or action.status != "executing":
            return
        if event_id is not None:
            self._end(action, "done")
        else:
            self._end(action, "failed", reason="exhausted")

    # --- the checks ----------------------------------------------------------

    def _check(
        self, action: _Action, tool: str, args: CreateEventInput, outsiders: list[str] | None
    ) -> str | None:
        """The first check that fails, as a key of `audit.REASONS`, or None.
        `outsiders` is the guest check's answer, read before the row was
        locked; None when the action only became new since."""
        if args.start_utc.tzinfo is None or args.end_utc.tzinfo is None:
            return "naive"
        if tool != action.tool:
            # A guest added or dropped since the approval turns one tool into
            # the other: not what the owner approved.
            return "mismatch"
        if tool == HOLD and args.attendees:
            return "hold_with_guests"
        if action.dry_run != self._writer.dry_run:
            return "mode"
        digest = args_hash(self._key, tool=tool, calendar_id=self._writer.calendar_id, args=args)
        if not hmac.compare_digest(digest, action.args_hash):
            return "mismatch"
        if TOOLS[tool] is Tier.EXTERNAL:
            # Read from Gmail and the contacts, never the graph state (D4).
            if outsiders is None:
                outsiders = self._outsiders(action.message_id, args.attendees)
            if outsiders:
                return "guests"
        return None

    def _status(self, action_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM outbound_actions WHERE id = %s", (action_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    # --- writing -------------------------------------------------------------

    def _start(self, action: _Action, args: CreateEventInput) -> _Action:
        """Store where the event goes, its id and the exact request."""
        started = replace(
            action,
            status="executing",
            calendar_id=self._writer.calendar_id,
            event_id=event_id_for(self._key, action.message_id, action.args_hash),
            request=event_body(
                title=args.title,
                start_utc=args.start_utc,
                end_utc=args.end_utc,
                timezone=args.timezone,
                attendees=args.attendees,
                location=args.location,
                description=args.description,
            ),
        )
        self._conn.execute(
            """
            UPDATE outbound_actions
               SET status = 'executing', calendar_id = %s, event_id = %s, request = %s,
                   started_at = now()
             WHERE id = %s AND status = 'approved'
            """,
            (started.calendar_id, started.event_id, Jsonb(started.request), action.id),
        )
        return started

    def _write(self, action: _Action, *, first: bool) -> Outcome:
        """Send the stored request, after asking whether an earlier attempt
        already did. The first attempt has nothing to ask about."""
        calendar_id, event_id, request = action.calendar_id, action.event_id, action.request
        assert calendar_id is not None and event_id is not None
        if not first and self._writer.find(calendar_id, event_id) is not None:
            return self._finish(action, "done")
        if self._writer.dry_run:
            return self._finish(action, "refused", reason="mode")
        if request is None:
            # The purge cleared it (D8): there is nothing left to send.
            return self._finish(action, "failed", reason="exhausted")
        try:
            made = self._writer.insert(calendar_id, request, event_id=event_id)
        except WriteRejectedError:
            # Google refused the request itself: another attempt would too.
            return self._finish(action, "failed", reason="provider")
        if made is None:
            # The client's own `DRY_RUN` guard: nothing was sent.
            return self._finish(action, "refused", reason="mode")
        return self._finish(action, "done")

    def _finish(
        self,
        action: _Action,
        status: Literal["done", "refused", "failed"],
        *,
        reason: str | None = None,
    ) -> Outcome:
        with self._conn.transaction():
            self._end(action, status, reason=reason)
        if status == "done":
            return Outcome("created", event_id=action.event_id)
        assert reason is not None
        return Outcome("refused", reason=audit.REASONS[reason])

    # --- recording -----------------------------------------------------------

    def _load(self, action_id: int, *, lock: bool) -> _Action | None:
        row = self._conn.execute(
            """
            SELECT id, decision_id, message_id, tool, tier, args_hash, dry_run, nonce,
                   status, calendar_id, event_id, request, reason
              FROM outbound_actions
             WHERE id = %s
            """
            + (" FOR UPDATE" if lock else ""),
            (action_id,),
        ).fetchone()
        return None if row is None else _Action(*row)

    def _refuse(self, action: _Action, reason: str) -> Outcome:
        self._end(action, "refused", reason=reason)
        return Outcome("refused", reason=audit.REASONS[reason])

    def _end(
        self,
        action: _Action,
        status: Literal["done", "dry_run", "refused", "failed"],
        *,
        reason: str | None = None,
    ) -> None:
        """Record how the action ended, and audit it, if it is still where
        this attempt left it. The stored request goes: it held the event's
        content only while the write was in flight."""
        phrase = audit.REASONS[reason] if reason is not None else None
        ended = self._conn.execute(
            """
            UPDATE outbound_actions
               SET status = %s, reason = %s, request = NULL, finished_at = now()
             WHERE id = %s AND status = %s
            """,
            (status, phrase, action.id, action.status),
        ).rowcount
        if not ended:
            return
        audit.record(
            self._conn,
            _KINDS[status],
            tool=action.tool,
            tier=action.tier,
            args_hash=action.args_hash,
            dry_run=action.dry_run,
            outcome=status,
            decision_id=action.decision_id,
            message_id=action.message_id,
            reason=phrase,
        )

    def _refuse_unbound(
        self,
        reason: str,
        *,
        message_id: str,
        tool: str | None = None,
        tier: Tier | None = None,
    ) -> Outcome:
        """A refusal with no action of this message's to record it on."""
        phrase = audit.REASONS[reason]
        audit.record(
            self._conn,
            "action_refused",
            tool=tool,
            tier=None if tier is None else int(tier),
            outcome="refused",
            message_id=message_id,
            reason=phrase,
        )
        return Outcome("refused", reason=phrase)
