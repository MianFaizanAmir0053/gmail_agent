"""Ingestion against a real Postgres. Skipped when none is reachable.

The exit criterion for M10 lives in `test_a_second_run_inserts_nothing`, and the
stronger claim -- that a second run also *costs* nothing -- lives in the
assertion about embed calls immediately below it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from app.config import Settings
from app.contracts import EmailMessage
from app.rag.ingest import DEFAULT_QUERY, collect, ingest

pytestmark = pytest.mark.integration

THREAD = "test-thread-rag"


class FakeMailbox:
    def __init__(self, messages: list[EmailMessage]) -> None:
        self._messages = {m.id: m for m in messages}

    def search(self, query: str, limit: int = 200) -> list[str]:
        return list(self._messages)[:limit]

    def get_message(self, message_id: str) -> EmailMessage:
        return self._messages[message_id]


class CountingModels:
    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls = 0

    def embed_content(self, **kwargs: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.1] * self.dimensions) for _ in kwargs["contents"]]
        )


class CountingClient:
    def __init__(self, dimensions: int) -> None:
        self.models = CountingModels(dimensions)


def email(message_id: str, body: str) -> EmailMessage:
    return EmailMessage(
        id=message_id,
        thread_id=THREAD,
        subject="Q3 planning offsite",
        body_text=body,
        sender="sara@example.com",
        recipients=["me@example.com"],
        received_at=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql://unused",
        gemini_api_key="unused",
        owner_email="me@example.com",
    )


@pytest.fixture
def rag_conn(migrated_database: str) -> Iterator[psycopg.Connection]:
    """A committing connection that cleans up only its own rows.

    `ingest` commits by design -- a long backfill that dies should keep what it
    already stored -- so the usual rolled-back fixture cannot be used here.
    Deleting by test thread and by run id leaves any real ingested data alone.
    """
    with psycopg.connect(migrated_database) as conn:
        row = conn.execute("SELECT coalesce(max(id), 0) FROM ingest_runs").fetchone()
        assert row is not None
        watermark = row[0]

        conn.execute("DELETE FROM chunks WHERE thread_id = %s", (THREAD,))
        conn.commit()
        try:
            yield conn
        finally:
            conn.rollback()
            conn.execute("DELETE FROM chunks WHERE thread_id = %s", (THREAD,))
            conn.execute("DELETE FROM ingest_runs WHERE id > %s", (watermark,))
            conn.commit()


# --- the exit criterion -----------------------------------------------------


def test_a_second_run_inserts_nothing(rag_conn: psycopg.Connection, settings: Settings) -> None:
    mailbox = FakeMailbox([email("m1", "Let's lock Thursday 3pm for the offsite in room 2.")])
    client = CountingClient(settings.embedding_dimensions)

    first = ingest(rag_conn, mailbox, client, settings=settings)
    assert first.chunks_inserted == 1

    second = ingest(rag_conn, mailbox, client, settings=settings)
    assert second.chunks_inserted == 0
    assert second.chunks_duplicate == 1


def test_a_second_run_also_costs_nothing(rag_conn: psycopg.Connection, settings: Settings) -> None:
    """Dedupe on insert alone would satisfy the criterion and still pay in full."""
    mailbox = FakeMailbox([email("m1", "Let's lock Thursday 3pm for the offsite in room 2.")])
    client = CountingClient(settings.embedding_dimensions)

    ingest(rag_conn, mailbox, client, settings=settings)
    after_first = client.models.calls

    second = ingest(rag_conn, mailbox, client, settings=settings)

    assert client.models.calls == after_first
    assert second.embed_calls == 0
    assert second.estimated_tokens == 0


# --- storage ----------------------------------------------------------------


def test_the_stored_row_carries_its_metadata(
    rag_conn: psycopg.Connection, settings: Settings
) -> None:
    mailbox = FakeMailbox([email("m1", "Let's lock Thursday 3pm for the offsite in room 2.")])
    ingest(rag_conn, mailbox, CountingClient(settings.embedding_dimensions), settings=settings)

    row = rag_conn.execute(
        "SELECT participants, subject, embedding_model, embedding IS NOT NULL, tsv IS NOT NULL"
        "  FROM chunks WHERE thread_id = %s",
        (THREAD,),
    ).fetchone()

    assert row is not None
    participants, subject, model, has_embedding, has_tsv = row
    assert participants == ["sara@example.com"]
    assert subject == "Q3 planning offsite"
    assert model == settings.embedding_model
    assert has_embedding and has_tsv


def test_the_subject_is_searchable_even_when_the_body_says_it(
    rag_conn: psycopg.Connection, settings: Settings
) -> None:
    """M11's keyword half depends on the generated column folding in the subject."""
    mailbox = FakeMailbox([email("m1", "Friday works for me, room 2 is already booked.")])
    ingest(rag_conn, mailbox, CountingClient(settings.embedding_dimensions), settings=settings)

    row = rag_conn.execute(
        "SELECT count(*) FROM chunks"
        " WHERE thread_id = %s AND tsv @@ plainto_tsquery('english', %s)",
        (THREAD, "offsite"),
    ).fetchone()
    assert row is not None and row[0] == 1


def test_a_width_mismatch_fails_before_anything_is_embedded(
    rag_conn: psycopg.Connection, settings: Settings
) -> None:
    wrong = settings.model_copy(update={"embedding_dimensions": 999})
    client = CountingClient(999)

    with pytest.raises(Exception, match="cannot be compared"):
        ingest(rag_conn, FakeMailbox([email("m1", "Thursday works.")]), client, settings=wrong)

    assert client.models.calls == 0


def test_a_failure_partway_keeps_what_it_already_paid_for(
    rag_conn: psycopg.Connection, settings: Settings
) -> None:
    """A backfill that dies must not discard embeddings it has already bought."""

    class DiesOnSecondBatch(CountingClient):
        def __init__(self, dimensions: int) -> None:
            super().__init__(dimensions)
            original = self.models.embed_content

            def embed_content(**kwargs: Any) -> Any:
                if self.models.calls >= 1:
                    raise RuntimeError("network died")
                return original(**kwargs)

            self.models.embed_content = embed_content  # type: ignore[method-assign]

    body = "Paragraph {i}. " + "word " * 400
    mailbox = FakeMailbox(
        [email(f"m{i}", body.replace("{i}", str(i))) for i in range(4)],
    )

    with pytest.raises(RuntimeError, match="network died"):
        ingest(
            rag_conn,
            mailbox,
            DiesOnSecondBatch(settings.embedding_dimensions),
            settings=settings,
            batch_size=2,
        )

    row = rag_conn.execute("SELECT count(*) FROM chunks WHERE thread_id = %s", (THREAD,)).fetchone()
    assert row is not None and row[0] == 2


def test_a_run_is_recorded(rag_conn: psycopg.Connection, settings: Settings) -> None:
    mailbox = FakeMailbox([email("m1", "Let's lock Thursday 3pm for the offsite in room 2.")])
    ingest(rag_conn, mailbox, CountingClient(settings.embedding_dimensions), settings=settings)

    row = rag_conn.execute(
        "SELECT status, chunks_inserted, estimated_cost_usd IS NOT NULL"
        "  FROM ingest_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "success"
    assert row[1] == 1
    assert row[2] is True


# --- collection -------------------------------------------------------------


def test_messages_with_nothing_original_are_counted_not_hidden() -> None:
    """A climbing count is how an over-eager cleaning rule announces itself."""
    mailbox = FakeMailbox(
        [
            email("m1", "Thursday at 3 works, room 2 is booked."),
            email("m2", "On Mon, someone wrote:\n> nothing new here at all\n"),
        ]
    )
    chunks, stats = collect(mailbox, query=DEFAULT_QUERY, limit=10, owner_email="me@example.com")

    assert stats.messages_seen == 2
    assert stats.messages_empty == 1
    assert len(chunks) == 1
