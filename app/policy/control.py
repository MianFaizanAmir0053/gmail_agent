"""The owner's switches (M17, D6).

One row, `control`: whether the agent is paused, and the spend gate's state.
Read here; Pause and Resume are recorded here too (17.12).
"""

from __future__ import annotations

import psycopg


def is_paused(conn: psycopg.Connection) -> bool:
    """Whether the owner has paused the agent. Nothing new runs while paused."""
    row = conn.execute("SELECT paused FROM control WHERE id = 1").fetchone()
    return bool(row is not None and row[0])
