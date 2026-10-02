"""The ingestion pipeline: Gmail -> clean -> chunk -> embed -> pgvector.

    python -m app.rag.ingest --limit 200
    python -m app.rag.ingest --limit 200 --plan     # no API calls, no writes

**Dedupe happens before embedding, not on insert.** `ON CONFLICT DO NOTHING`
alone would satisfy the letter of the exit criterion -- a second run inserts
zero rows -- while costing exactly as much as the first, because every chunk was
embedded on the way to being discarded. Hashing first and asking the database
which hashes it already has turns a re-run into one SELECT.

`--plan` exists for the same reason. Cleaning and chunking are the parts most
likely to be wrong, and being able to inspect the chunks a run *would* produce,
without spending anything, is what makes the "spot-check ten chunks by hand"
step of M10 cheap enough to actually do.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import psycopg

from app.config import Settings, get_settings
from app.contracts import EmailMessage
from app.rag.chunk import Chunk, chunk_message
from app.rag.embed import (
    BATCH_SIZE,
    EmbeddingError,
    Pacer,
    column_dimensions,
    embed_documents,
    estimated_tokens,
    to_pgvector,
)

DEFAULT_QUERY = "-in:chats -category:promotions -category:social -category:updates"
"""What is worth remembering.

Promotions and updates are the bulk of a personal mailbox and none of it is
context about a person or a commitment. Excluding them at the Gmail query is far
cheaper than embedding them and learning the same thing from search results.
"""

EMBEDDING_RATE_PER_MTOK = Decimal("0.15")
"""USD per million tokens for `gemini-embedding-001`, as published.

Kept here rather than in `app.obs.pricing` on purpose: that table prices calls
whose token counts the API reported. This one multiplies an *estimate*, and
mixing the two would let an estimate be summed alongside measurements as though
it carried the same weight.
"""


class MailboxLike(Protocol):
    def search(self, query: str, limit: int = ...) -> list[str]: ...
    def get_message(self, message_id: str) -> EmailMessage: ...


@dataclass(slots=True)
class Stats:
    messages_seen: int = 0
    messages_empty: int = 0
    chunks_produced: int = 0
    chunks_duplicate: int = 0
    chunks_inserted: int = 0
    embed_calls: int = 0
    estimated_tokens: int = 0
    skipped_messages: list[str] = field(default_factory=list)

    @property
    def estimated_cost_usd(self) -> Decimal:
        return (
            Decimal(self.estimated_tokens) * EMBEDDING_RATE_PER_MTOK / Decimal(1_000_000)
        ).quantize(Decimal("0.000001"))


def existing_hashes(conn: psycopg.Connection, hashes: list[str]) -> set[str]:
    """Which of these are already stored.

    One query for the whole batch. Asking per chunk would be a round trip per
    chunk, and the re-run case -- where every hash is already present -- is the
    one that has to stay fast.
    """
    if not hashes:
        return set()
    rows = conn.execute(
        "SELECT content_hash FROM chunks WHERE content_hash = ANY(%s)", (hashes,)
    ).fetchall()
    return {row[0] for row in rows}


def insert_chunks(
    conn: psycopg.Connection,
    chunks: list[Chunk],
    vectors: list[list[float]],
    model: str,
) -> int:
    """Store chunks with their embeddings. Returns rows actually inserted."""
    if len(chunks) != len(vectors):
        raise EmbeddingError(
            f"{len(vectors)} vectors for {len(chunks)} chunks -- refusing to insert misaligned rows"
        )

    inserted = 0
    for chunk, vector in zip(chunks, vectors, strict=True):
        row = conn.execute(
            """
            INSERT INTO chunks (
                thread_id, message_id, ordinal, subject, content, content_hash,
                participants, sent_at, embedding, embedding_model
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (
                chunk.thread_id,
                chunk.message_id,
                chunk.ordinal,
                chunk.subject,
                chunk.content,
                chunk.content_hash,
                list(chunk.participants),
                chunk.sent_at,
                to_pgvector(vector),
                model,
            ),
        ).fetchone()
        if row is not None:
            inserted += 1

    return inserted


def collect(
    mailbox: MailboxLike, *, query: str, limit: int, owner_email: str
) -> tuple[
    list[Chunk],
    Stats,
]:
    """Fetch, clean, and chunk. No embedding, no writes -- this is the whole
    pipeline up to the point where it starts costing money."""
    stats = Stats()
    chunks: list[Chunk] = []

    for message_id in mailbox.search(query, limit=limit):
        stats.messages_seen += 1
        message = mailbox.get_message(message_id)
        produced = chunk_message(message, owner_email=owner_email)

        if not produced:
            # Either an attachment-only message or one whose body was entirely
            # quoted history. Both are legitimate; a count that climbs towards
            # messages_seen is how an over-eager cleaning rule announces itself.
            stats.messages_empty += 1
            stats.skipped_messages.append(message_id)
            continue

        chunks.extend(produced)

    stats.chunks_produced = len(chunks)
    return chunks, stats


def ingest(
    conn: psycopg.Connection,
    mailbox: MailboxLike,
    client: Any,
    *,
    settings: Settings,
    query: str = DEFAULT_QUERY,
    limit: int = 200,
    batch_size: int = BATCH_SIZE,
    may_continue: Callable[[], bool] | None = None,
) -> Stats:
    dimensions = column_dimensions(conn)
    if dimensions != settings.embedding_dimensions:
        raise EmbeddingError(
            f"chunks.embedding is vector({dimensions}) but EMBEDDING_DIMENSIONS is "
            f"{settings.embedding_dimensions}. Vectors of different widths cannot be "
            "compared, so this must be reconciled before anything is embedded."
        )

    run_id = _start_run(conn, query, settings.embedding_model)

    stats = Stats()
    pacer = Pacer()

    try:
        chunks, stats = collect(mailbox, query=query, limit=limit, owner_email=settings.owner_email)

        seen = existing_hashes(conn, [c.content_hash for c in chunks])
        fresh = [c for c in chunks if c.content_hash not in seen]
        stats.chunks_duplicate = len(chunks) - len(fresh)

        # Committed a batch at a time rather than once at the end. A backfill
        # that dies two thirds of the way through used to roll back every chunk
        # it had already paid to embed; now the next run's dedupe skips them and
        # it resumes from where the failure was.
        for start in range(0, len(fresh), batch_size):
            if may_continue is not None and not may_continue():
                # The spending cap (M17, D5): the rest waits for the next run,
                # whose dedupe skips what this one already embedded.
                break
            group = fresh[start : start + batch_size]
            texts = [chunk.embedding_text() for chunk in group]

            vectors, calls = embed_documents(
                client,
                texts,
                model=settings.embedding_model,
                dimensions=dimensions,
                batch_size=batch_size,
                pacer=pacer,
            )

            stats.embed_calls += calls
            stats.estimated_tokens += estimated_tokens(texts)
            stats.chunks_inserted += insert_chunks(conn, group, vectors, settings.embedding_model)
            conn.commit()

        _finish_run(conn, run_id, stats, status="success")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        _finish_run(conn, run_id, stats, status="failed", error=f"{type(exc).__name__}: {exc}")
        conn.commit()
        raise

    return stats


def _start_run(conn: psycopg.Connection, query: str, model: str) -> int:
    row = conn.execute(
        "INSERT INTO ingest_runs (query, embedding_model) VALUES (%s, %s) RETURNING id",
        (query, model),
    ).fetchone()
    assert row is not None
    # Committed immediately: a run that dies hard should still leave a record
    # that it started, and the failure path below rolls back the chunk inserts.
    conn.commit()
    return int(row[0])


def _finish_run(
    conn: psycopg.Connection,
    run_id: int,
    stats: Stats,
    *,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE ingest_runs
           SET ended_at = now(), status = %s, error = %s,
               messages_seen = %s, messages_empty = %s,
               chunks_produced = %s, chunks_duplicate = %s, chunks_inserted = %s,
               embed_calls = %s, estimated_tokens = %s, estimated_cost_usd = %s
         WHERE id = %s
        """,
        (
            status,
            error,
            stats.messages_seen,
            stats.messages_empty,
            stats.chunks_produced,
            stats.chunks_duplicate,
            stats.chunks_inserted,
            stats.embed_calls,
            stats.estimated_tokens,
            stats.estimated_cost_usd,
            run_id,
        ),
    )


def _mailbox(settings: Settings) -> MailboxLike:
    from app.google.auth import build_service, load_credentials
    from app.google.gmail import GmailClient

    credentials = load_credentials(settings)
    return GmailClient(build_service("gmail", "v1", credentials))


def _report(stats: Stats, *, planned: bool) -> None:
    verb = "would produce" if planned else "produced"
    print(f"\n  messages seen      {stats.messages_seen}")
    print(f"  no original text   {stats.messages_empty}")
    print(f"  chunks {verb:<12} {stats.chunks_produced}")
    if not planned:
        print(f"  already stored     {stats.chunks_duplicate}")
        print(f"  inserted           {stats.chunks_inserted}")
        print(f"  embed requests     {stats.embed_calls}")
        print(
            f"  estimated cost     ${stats.estimated_cost_usd} "
            f"(~{stats.estimated_tokens} tokens; the API reports no usage)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest past email into pgvector.")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Gmail search query")
    parser.add_argument("--limit", type=int, default=200, help="Maximum messages to fetch")
    parser.add_argument("--batch", type=int, default=BATCH_SIZE, help="Chunks per embed request")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Clean and chunk only. No embeddings, no writes, no cost.",
    )
    parser.add_argument("--show", type=int, default=0, help="Print the first N chunks in full")
    args = parser.parse_args()

    settings = get_settings()
    mailbox = _mailbox(settings)

    if args.plan:
        chunks, stats = collect(
            mailbox, query=args.query, limit=args.limit, owner_email=settings.owner_email
        )
        for chunk in chunks[: args.show]:
            print(f"\n--- {chunk.message_id}#{chunk.ordinal} · {len(chunk.content)} chars ---")
            print(chunk.embedding_text())
        _report(stats, planned=True)
        return

    from app.policy import models
    from app.store.db import connect

    client = models.client(settings, models.local_gate(settings))

    with connect(settings.database_url) as conn:
        stats = ingest(
            conn,
            mailbox,
            client,
            settings=settings,
            query=args.query,
            limit=args.limit,
            batch_size=args.batch,
        )

    _report(stats, planned=False)


if __name__ == "__main__":
    main()
