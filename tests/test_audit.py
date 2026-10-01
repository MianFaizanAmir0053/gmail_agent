"""The audit log (M17, D7): every attempt recorded, nothing quoted, nothing
rewritten."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from app.policy import audit

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "010_action_policy.sql"

NO_DATABASE = None
"""`record` refuses before it touches Postgres, so these tests need none."""


@pytest.mark.parametrize(
    "fields",
    [
        {"reason": "Salary review with Sara at 4pm"},
        {"outcome": "created event 'Therapy, room 4B'"},
        {"tool": "gmail.send"},
    ],
)
def test_only_fixed_words_are_accepted(fields: dict[str, str]) -> None:
    """A free-text field is where email content would leak in. Every word the
    log holds comes from a closed list in code."""
    with pytest.raises(ValueError):
        audit.record(NO_DATABASE, "action_refused", **fields)  # type: ignore[arg-type]


@pytest.mark.integration
def test_a_record_is_written(conn: psycopg.Connection) -> None:
    audit.record(
        conn,
        "action_refused",
        tool="calendar.create_hold",
        tier=1,
        args_hash="a" * 64,
        dry_run=True,
        outcome="refused",
        decision_id=7,
        message_id="m1",
        reason=audit.REASONS["changed"],
    )

    row = conn.execute(
        "SELECT kind, tool, tier, outcome, reason FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row == (
        "action_refused",
        "calendar.create_hold",
        1,
        "refused",
        audit.REASONS["changed"],
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_log SET outcome = 'done'", "DELETE FROM audit_log"],
)
def test_nothing_is_rewritten(conn: psycopg.Connection, statement: str) -> None:
    """Kept for good, as M24's evidence: code can add to it and never alter it."""
    audit.record(conn, "paused")
    with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
        conn.execute(statement)


@pytest.mark.integration
def test_the_migration_can_be_run_again(conn: psycopg.Connection) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)


@pytest.mark.integration
def test_one_control_row_starts_unpaused(conn: psycopg.Connection) -> None:
    rows = conn.execute("SELECT id, paused, budget_state FROM control").fetchall()
    assert rows == [(1, False, "ok")]


@pytest.mark.integration
def test_an_action_cannot_be_executing_without_its_event_id(conn: psycopg.Connection) -> None:
    """The id is what lets a later attempt find the event instead of booking
    a second one (D3), so a row cannot claim to be executing without it."""
    conn.execute(
        "INSERT INTO processed_messages (gmail_message_id, thread_id, status)"
        " VALUES ('m1', 'm1', 'awaiting_approval')"
    )
    conn.execute(
        "INSERT INTO proposals (message_id, revision, status, action_type,"
        " pipeline_version, dry_run) VALUES ('m1', 1, 'deciding', 'calendar_hold', 'v', true)"
    )
    decision = conn.execute(
        "INSERT INTO decisions (message_id, revision, action, via, action_type,"
        " pipeline_version, latency_seconds) VALUES ('m1', 1, 'confirm', 'web',"
        " 'calendar_hold', 'v', 0) RETURNING id"
    ).fetchone()
    assert decision is not None

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO outbound_actions (decision_id, message_id, tool, tier, args_hash,"
            " dry_run, nonce, status) VALUES (%s, 'm1', 'calendar.create_hold', 1, 'h',"
            " true, 'n', 'executing')",
            (decision[0],),
        )
