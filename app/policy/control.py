"""The owner's switches (M17, D6).

One row, `control`: whether the agent is paused, when and from where that
last changed, and the spend gate's state (`app/policy/budget.py`).

While paused, new work stops: poll claims nothing, the worker applies no
decision but still carries out the owner's withdraw requests, ingestion
claims nothing, and the registry refuses new actions (`PausedError`). Reads,
reconciliation, the purge, the token check and the mail sync carry on: none
calls a model. Pause and Resume are each audited when they change something.

One function, `switch`, for both: `.resume(` is kept for the graph's
resume, which only the worker may call (`tests/test_one_resumer.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg

from app.policy import audit

Via = Literal["web", "cli"]


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
        raise RuntimeError("the control row is missing: run the migrations")
    return Control(*row)


def is_paused(conn: psycopg.Connection) -> bool:
    """Whether the owner has paused the agent. Nothing new runs while paused."""
    row = conn.execute("SELECT paused FROM control WHERE id = 1").fetchone()
    return bool(row is not None and row[0])


def switch(conn: psycopg.Connection, *, paused: bool, via: Via) -> bool:
    """Pause the agent, or resume it. Returns whether this changed anything:
    pausing a paused agent is not audited again. A decision held while
    paused is due as soon as the agent resumes."""
    with conn.transaction():
        changed = conn.execute(
            """
            UPDATE control SET paused = %s, changed_at = now(), changed_via = %s
             WHERE id = 1 AND paused <> %s
            RETURNING id
            """,
            (paused, via, paused),
        ).fetchone()
        if changed is not None:
            audit.record(conn, "paused" if paused else "resumed")
    return changed is not None
