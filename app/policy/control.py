"""The owner's switches (M17, D6).

One row, `control`: whether the agent is paused, when and from where that
last changed, and the spend gate's state (`app/policy/budget.py`).

While paused, new work stops: poll claims nothing, the worker applies no
decision but still carries out the owner's withdraw requests, ingestion
claims nothing, and the registry refuses new actions (`PausedError`). Reads,
reconciliation, the purge, the token check and the mail sync carry on: none
calls a model. Pause and Resume are each audited when they change something,
with where the change came from.

One function, `switch`, for both: `.resume(` is kept for the graph's
resume, which only the worker may call (`tests/test_one_resumer.py`).

A missing row is an error, never "not paused": read that way, it would let
everything run, and Pause would answer that it had worked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg

from app.policy import audit

Via = Literal["web", "cli"]

MISSING = "the control row is missing: run the migrations"


@dataclass(frozen=True, slots=True)
class Control:
    paused: bool
    changed_at: datetime
    changed_via: str | None
    budget_state: str


def read(conn: psycopg.Connection) -> Control:
    row = conn.execute(
        "SELECT paused, changed_at, changed_via, budget_state FROM control WHERE id = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError(MISSING)
    return Control(*row)


def is_paused(conn: psycopg.Connection) -> bool:
    """Whether the owner has paused the agent. Nothing new runs while paused."""
    row = conn.execute("SELECT paused FROM control WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError(MISSING)
    return bool(row[0])


def switch(conn: psycopg.Connection, *, paused: bool, via: Via) -> bool:
    """Pause the agent, or resume it. Returns whether this changed anything:
    pausing a paused agent is not audited again.

    Resume moves each decision the pause held on by the length of the pause,
    never past now. One that fell due during the pause is due at once. One
    that was already overdue before the pause stays as overdue as it was, so
    `/health`'s clock for a stuck queue keeps counting it
    (`app.jobs.scheduler.decisions_status`), and a quick pause and resume
    cannot hide it.
    """
    with conn.transaction():
        row = conn.execute(
            "SELECT paused, changed_at FROM control WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if row is None:
            raise RuntimeError(MISSING)
        was_paused, since = row
        if was_paused == paused:
            return False
        conn.execute(
            "UPDATE control SET paused = %s, changed_at = now(), changed_via = %s WHERE id = 1",
            (paused, via),
        )
        audit.record(conn, "paused" if paused else "resumed", reason=audit.REASONS[f"via_{via}"])
        if not paused:
            conn.execute(
                """
                UPDATE decisions
                   SET next_attempt_at = least(next_attempt_at + (now() - %s), now())
                 WHERE outcome IS NULL AND next_attempt_at < now()
                """,
                (since,),
            )
    return True
