"""The web app's database role (M16, D5): it reads what the app shows, and
nothing else, and writes nothing at all.

The role is borrowed with `SET ROLE` inside the rolled-back test transaction,
so no password is involved and nothing outlives the test.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "008_web_reader.sql"

SHOWN = (
    "runs",
    "spans",
    "eval_runs",
    "proposals",
    "decisions",
    "control",
    "confirmed_contacts",
    "audit_log",
)
"""What the web app reads: the analytics pages, the timeline, the header's
pause and budget state, the guests already allowed, and the Activity page."""

HIDDEN = (
    "processed_messages",
    "checkpoints",
    "push_subscriptions",
    "alerts_sent",
    "pairing_codes",
    "job_runs",
    "outbound_actions",
    "model_spend",
    "gmail_messages",
    "gmail_cursors",
    "gmail_fetch_queue",
)
"""Everything else: the mailbox side, the graph's state, the secrets of push,
the approvals' nonces, and the spend record. The mail sync's records (M20)
are the mailbox side too: the web app shows none of them."""


def _as_web_reader(conn: psycopg.Connection) -> None:
    conn.execute("GRANT web_reader TO CURRENT_USER")
    conn.execute("SET ROLE web_reader")


@pytest.mark.integration
def test_the_migration_can_be_run_again(conn: psycopg.Connection) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)


@pytest.mark.integration
def test_the_role_cannot_log_in_until_the_owner_gives_it_a_password(
    conn: psycopg.Connection,
) -> None:
    """The password is set once, by hand, in Supabase: never in the repo."""
    row = conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'web_reader'").fetchone()
    assert row == (False,)


@pytest.mark.integration
@pytest.mark.parametrize("table", SHOWN)
def test_the_web_reader_reads_what_the_app_shows(conn: psycopg.Connection, table: str) -> None:
    _as_web_reader(conn)
    conn.execute(f"SELECT count(*) FROM {table}")


@pytest.mark.integration
@pytest.mark.parametrize("table", HIDDEN)
def test_the_web_reader_reads_nothing_else(conn: psycopg.Connection, table: str) -> None:
    _as_web_reader(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute(f"SELECT count(*) FROM {table}")


@pytest.mark.integration
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO decisions (message_id, revision, action, via, action_type, "
        "pipeline_version, latency_seconds) VALUES ('m1', 1, 'confirm', 'web', "
        "'calendar_hold', 'v', 0)",
        "UPDATE proposals SET status = 'decided'",
        "DELETE FROM runs",
    ],
)
def test_the_web_reader_cannot_write(conn: psycopg.Connection, statement: str) -> None:
    """Every write goes through the Fly API, where `decide()` holds the rules."""
    _as_web_reader(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute(statement)


# --- chat turns (M19, D12) -------------------------------------------------------------

CHAT_MIGRATION = MIGRATION.parent / "013_chat.sql"

READABLE_TURN = ("id", "status", "asked_at", "typed", "answer", "reason")
"""What the chat page shows: a turn's state, the request, the answer, and why
it stopped. Decision 12 widened M16's rule for exactly these."""

UNREADABLE_TURN = ("pasted", "plan", "idempotency_key", "started_at", "finished_at")


@pytest.mark.integration
def test_the_chat_migration_can_be_run_again(conn: psycopg.Connection) -> None:
    sql = CHAT_MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)


def _as_web_reader_after_the_chat_migration(conn: psycopg.Connection) -> None:
    """The migration as the file says it now, then the role: the database
    applied it once, and a test of the grant must test the file."""
    conn.execute(CHAT_MIGRATION.read_text(encoding="utf-8"))
    _as_web_reader(conn)


@pytest.mark.integration
def test_the_web_reader_reads_a_turns_granted_columns(conn: psycopg.Connection) -> None:
    _as_web_reader_after_the_chat_migration(conn)
    conn.execute(f"SELECT {', '.join(READABLE_TURN)} FROM chat_turns")


@pytest.mark.integration
@pytest.mark.parametrize("column", UNREADABLE_TURN)
def test_the_web_reader_never_reads_the_paste_or_the_plan(
    conn: psycopg.Connection, column: str
) -> None:
    _as_web_reader_after_the_chat_migration(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute(f"SELECT {column} FROM chat_turns")


@pytest.mark.integration
def test_the_web_reader_cannot_store_a_turn(conn: psycopg.Connection) -> None:
    _as_web_reader_after_the_chat_migration(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute(
            "INSERT INTO chat_turns (idempotency_key, typed) VALUES ('k' || repeat('x', 20), 'hi')"
        )
