"""Supabase's Data API gets nothing (migration 009).

On Supabase, every table created in `public` is granted to `anon` and
`authenticated`, the roles its REST and GraphQL endpoints act as. This app
uses neither endpoint, and the project's anon key would otherwise reach the
ledger, the proposals and the checkpoints, which hold whole emails.

The roles exist only on Supabase, so these tests create them inside the
rolled-back transaction, with the grants Supabase would have given them.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

pytestmark = pytest.mark.integration

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "009_no_data_api.sql"

API_ROLES = ("anon", "authenticated")


def _as_supabase_grants_them(conn: psycopg.Connection) -> None:
    for role in API_ROLES:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        if exists is None:
            conn.execute(f"CREATE ROLE {role} NOLOGIN")
        conn.execute(f"GRANT ALL ON processed_messages, proposals, decisions TO {role}")
        conn.execute(f"GRANT ALL ON SEQUENCE decisions_id_seq TO {role}")
        conn.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}")
        conn.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {role}")
        # The global form too: a schema's defaults cannot take it back.
        conn.execute(f"ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO {role}")
        conn.execute(f"ALTER DEFAULT PRIVILEGES GRANT ALL ON SEQUENCES TO {role}")


def _can(conn: psycopg.Connection, query: str, *args: str) -> bool:
    row = conn.execute(query, args).fetchone()
    assert row is not None
    return bool(row[0])


def test_the_api_roles_lose_every_table_and_sequence(conn: psycopg.Connection) -> None:
    _as_supabase_grants_them(conn)

    conn.execute(MIGRATION.read_text(encoding="utf-8"))

    for role in API_ROLES:
        for table in ("processed_messages", "proposals", "decisions"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not _can(
                    conn, "SELECT has_table_privilege(%s, %s, %s)", role, table, privilege
                ), f"{role} can {privilege} {table}"
        assert not _can(
            conn, "SELECT has_sequence_privilege(%s, 'decisions_id_seq', 'USAGE')", role
        )


def test_tables_created_later_are_not_granted_either(conn: psycopg.Connection) -> None:
    """LangGraph creates the checkpoint tables at run time, after migrations."""
    _as_supabase_grants_them(conn)

    conn.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.execute("CREATE TABLE created_after_009 (id BIGSERIAL PRIMARY KEY)")

    for role in API_ROLES:
        assert not _can(conn, "SELECT has_table_privilege(%s, 'created_after_009', 'SELECT')", role)
        assert not _can(
            conn, "SELECT has_sequence_privilege(%s, 'created_after_009_id_seq', 'USAGE')", role
        )


def test_the_migration_can_run_again_and_without_the_roles(conn: psycopg.Connection) -> None:
    """Local Postgres and Neon have no such roles; there it changes nothing."""
    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)
