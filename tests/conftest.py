from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlsplit

import psycopg
import pytest

from app.config import transaction_pooler_problem
from app.store.db import migrate

DEFAULT_TEST_DSN = "postgresql://mailagent:mailagent@localhost:5432/mailagent"


def _where(url: str) -> str:
    """Host and port only. A hosted test database's URL carries its password,
    and skip reasons are printed in every test run's output."""
    if "://" not in url:
        return "(key=value DSN)"
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or 5432}"


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DSN)
    if problem := transaction_pooler_problem(url):
        pytest.fail(f"TEST_DATABASE_URL {problem}")
    return url


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """Apply migrations once, or skip the whole integration suite.

    Ledger behaviour is Postgres behaviour -- ON CONFLICT, CHECK constraints,
    ANY(array). Testing it against SQLite would test a different database and
    prove nothing about the one that runs in production, so these tests need a
    real server and skip cleanly when there isn't one.
    """
    try:
        with psycopg.connect(database_url, connect_timeout=3) as conn:
            migrate(conn)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres unavailable at {_where(database_url)}: {exc}")
    return database_url


@pytest.fixture
def conn(migrated_database: str) -> Iterator[psycopg.Connection]:
    """A connection on a clean slate, rolled back afterwards.

    Both halves matter. The rollback stops tests leaking into each other; the
    upfront clear stops *real* rows leaking into tests -- the same database
    serves `app.jobs.poll` during development, and a test asserting "the cursor
    starts empty" would otherwise fail purely because someone ran the poller.
    Because it is all inside the rolled-back transaction, that development data
    survives untouched.
    """
    with psycopg.connect(migrated_database) as connection:
        # Decisions refuse cascading deletes (M24's evidence), and their
        # approvals refuse them in turn (M17), so those go first -- still
        # inside the transaction that is rolled back.
        connection.execute("DELETE FROM outbound_actions")
        connection.execute("DELETE FROM decisions")
        connection.execute("DELETE FROM processed_messages")
        connection.execute("UPDATE sync_state SET last_history_id = NULL WHERE id = 1")
        yield connection
        connection.rollback()
