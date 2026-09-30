"""Connections and migrations.

Plain SQL files applied in filename order, with an `applied_migrations` table so
reruns are no-ops. Alembic is the right answer once the schema starts churning;
until then it is a dependency and a directory of generated files in exchange for
nothing.

    python -m app.store.db          # apply pending migrations
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path("migrations")


@contextmanager
def connect(
    database_url: str, *, connect_timeout: int | None = None
) -> Iterator[psycopg.Connection]:
    """A committed-on-success, rolled-back-on-error connection.

    `connect_timeout` is in seconds. Without one, an unreachable host can hold
    the caller for as long as the operating system keeps retrying -- minutes
    on Windows.
    """
    if connect_timeout is None:
        with psycopg.connect(database_url) as conn:
            yield conn
    else:
        with psycopg.connect(database_url, connect_timeout=connect_timeout) as conn:
            yield conn


def _ensure_registry(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applied_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def applied(conn: psycopg.Connection) -> set[str]:
    _ensure_registry(conn)
    return {row[0] for row in conn.execute("SELECT filename FROM applied_migrations")}


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations in filename order. Returns what ran."""
    done = applied(conn)
    ran: list[str] = []

    for path in sorted(directory.glob("*.sql")):
        if path.name in done:
            continue
        # Each migration and its registry row commit together, so a crash
        # mid-run cannot leave a migration applied but unrecorded.
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO applied_migrations (filename) VALUES (%s)", (path.name,))
        ran.append(path.name)

    return ran


def main() -> None:
    from app.config import get_settings

    with connect(get_settings().database_url) as conn:
        ran = migrate(conn)

    print(f"Applied {len(ran)} migration(s): {', '.join(ran) or 'none pending'}")


if __name__ == "__main__":
    main()
