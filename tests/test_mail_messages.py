"""The mail sync's records (M20, D1, D2 and D8).

Metadata only: the table has no column a subject, snippet or body could go
in. The records are Postgres behaviour -- CHECK constraints, ON CONFLICT --
so they are tested against a real server.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "011_mail_sync.sql"


def _columns(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
    ).fetchall()
    return {row[0] for row in rows}


# --- the migration (20.1) -----------------------------------------------------


@pytest.mark.integration
def test_the_migration_can_be_run_again(conn: psycopg.Connection) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)


@pytest.mark.integration
def test_no_column_could_hold_content(conn: psycopg.Connection) -> None:
    """No subject, snippet or body until M18 can strip one-time codes."""
    for table in ("gmail_messages", "gmail_cursors", "gmail_fetch_queue"):
        columns = _columns(conn, table)
        assert columns, f"{table} is missing"
        assert not columns & {"subject", "snippet", "body", "body_text", "payload"}


@pytest.mark.integration
def test_the_records_have_the_columns_the_spec_names(conn: psycopg.Connection) -> None:
    assert {
        "account",
        "message_id",
        "thread_id",
        "internal_at",
        "label_ids",
        "direction",
        "to_self",
        "category",
        "from_addr",
        "to_addrs",
        "cc_addrs",
        "has_list_unsubscribe",
        "precedence",
        "auto_submitted",
        "arrived_via",
        "first_seen_at",
        "updated_at",
        "gone_at",
    } <= _columns(conn, "gmail_messages")
    assert {
        "account",
        "history_id",
        "feed_from",
        "switch_over_at",
        "caught_up_at",
        "backfill_until",
        "gap_from",
        "gap_until",
        "gap_progress",
        "catch_ups",
    } <= _columns(conn, "gmail_cursors")
    assert {"message_id", "reason", "queued_at", "strikes", "status"} <= _columns(
        conn, "gmail_fetch_queue"
    )


def _insert_message(conn: psycopg.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "account": "me@example.com",
        "message_id": "m1",
        "thread_id": "t1",
        "internal_at": "2026-10-01T09:00:00Z",
        "direction": "in",
        "to_self": False,
        "category": "primary",
        "has_list_unsubscribe": False,
        "arrived_via": "history",
    } | overrides
    columns = ", ".join(row)
    placeholders = ", ".join(f"%({name})s" for name in row)
    conn.execute(f"INSERT INTO gmail_messages ({columns}) VALUES ({placeholders})", row)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("direction", "sideways"),
        ("category", "spam"),
        ("arrived_via", "magic"),
    ],
)
def test_the_closed_lists_are_enforced(conn: psycopg.Connection, column: str, value: str) -> None:
    conn.execute("DELETE FROM gmail_messages")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _insert_message(conn, **{column: value})


@pytest.mark.integration
def test_a_message_has_one_row_per_account(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM gmail_messages")
    _insert_message(conn)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _insert_message(conn)


@pytest.mark.integration
def test_a_gap_is_recorded_whole_or_not_at_all(conn: psycopg.Connection) -> None:
    """`gap_from` and `gap_until` mean nothing apart."""
    conn.execute("DELETE FROM gmail_cursors")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            """
            INSERT INTO gmail_cursors (account, history_id, feed_from, backfill_until, gap_from)
            VALUES ('me@example.com', '1', now(), now(), now())
            """
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("column", "value"),
    [("status", "maybe"), ("reason", "whim"), ("strikes", -1)],
)
def test_the_fetch_queue_takes_only_its_own_words(
    conn: psycopg.Connection, column: str, value: object
) -> None:
    conn.execute("DELETE FROM gmail_fetch_queue")
    row: dict[str, object] = {"message_id": "m1", "reason": "label_change"} | {column: value}
    columns = ", ".join(row)
    placeholders = ", ".join(f"%({name})s" for name in row)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(f"INSERT INTO gmail_fetch_queue ({columns}) VALUES ({placeholders})", row)
