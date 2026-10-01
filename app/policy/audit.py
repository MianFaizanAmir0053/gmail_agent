"""The audit log (M17, D7).

Every attempt to act, refusals included, and every change to the owner's
switches, recorded for good as M24's evidence. It must never quote an email,
so nothing free-form reaches it: each word comes from a closed list here, and
anything else is refused before Postgres is touched. Ids and keyed hashes
link a row to what it was about without spelling that out.

Rows are never rewritten: a trigger in `010_action_policy.sql` rejects
`UPDATE` and `DELETE`.
"""

from __future__ import annotations

from typing import Literal, get_args

import psycopg

Kind = Literal[
    "action_approved",
    "action_executed",
    "action_refused",
    "action_failed",
    "decision_withdrawn",
    "withdraw_declined",
    "proposal_expired",
    "paused",
    "resumed",
    "budget_warning",
    "budget_exhausted",
    "budget_ok",
    "message_too_costly",
    "contact_allowed",
    "contact_removed",
    "write_unconfirmed",
]

TOOLS = frozenset({"calendar.freebusy", "calendar.create_hold", "calendar.create_invite"})
"""Every tool the registry may name. `tests/test_registry.py` holds the two
lists together."""

OUTCOMES = frozenset({"approved", "done", "dry_run", "refused", "failed", "withdrawn", "declined"})

REASONS: dict[str, str] = {
    "mode": "made under another mode",
    "changed": "the proposal changed",
    "guests": "guests outside the thread",
    "mismatch": "arguments differ from the approval",
    "nonce": "approval does not match",
    "used": "approval already used",
    "not_ready": "not ready",
    "unknown_tool": "unknown tool",
    "hold_with_guests": "a hold cannot have guests",
    "already_applied": "already being applied",
    "withdrawn": "withdrawn by the owner",
    "unconfirmed": "the calendar write could not be confirmed",
    "provider": "the calendar refused the write",
    "legacy": "approve again",
    "too_costly": "too costly to read",
    "exhausted": "attempts exhausted",
    "naive": "a time without a zone",
}
"""The only reasons a row can give. Fixed phrases, so a statistic can count
them and no email can hide in one."""


def record(
    conn: psycopg.Connection | None,
    kind: Kind,
    *,
    tool: str | None = None,
    tier: int | None = None,
    args_hash: str | None = None,
    dry_run: bool | None = None,
    outcome: str | None = None,
    decision_id: int | None = None,
    message_id: str | None = None,
    subject_hash: str | None = None,
    reason: str | None = None,
) -> None:
    """Append one row. Raises `ValueError` for any word not on the lists above,
    before the database is touched."""
    if kind not in get_args(Kind):
        raise ValueError("unknown kind for the audit log")
    if tool is not None and tool not in TOOLS:
        raise ValueError("unknown tool for the audit log")
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError("unknown outcome for the audit log")
    if reason is not None and reason not in REASONS.values():
        raise ValueError("the audit log takes only its fixed reasons")
    assert conn is not None
    conn.execute(
        """
        INSERT INTO audit_log (kind, tool, tier, args_hash, dry_run, outcome,
                               decision_id, message_id, subject_hash, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            kind,
            tool,
            tier,
            args_hash,
            dry_run,
            outcome,
            decision_id,
            message_id,
            subject_hash,
            reason,
        ),
    )
