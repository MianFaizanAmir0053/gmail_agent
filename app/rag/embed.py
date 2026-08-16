"""Embedding chunks and queries.

Three things about this API are not obvious from its signature, and each of them
produces a corpus that is wrong rather than an error that is loud.

**The SDK's batch coercion is model-dependent.** Passing `list[str]` to
`embed_content` returns one vector per string on `gemini-embedding-001`, but a
*single* vector on `gemini-embedding-2` -- the bare strings are packed into one
multi-part Content, which the newer model reads as one document. Three chunks in,
one vector out, and code that zips inputs to outputs then files every embedding
against the wrong chunk. Passing an explicit `list[Content]` behaves identically
on both, so that is what this module does, and `_check_alignment` refuses to
return a mismatched batch under any circumstances.

**Dimensions below 3072 come back un-normalised.** `gemini-embedding-001` is
Matryoshka-trained, so a 1536-wide prefix is a genuine embedding -- but its norm
is around 0.69, not 1. Cosine distance does not care, being scale-invariant, so
this survives contact with pgvector unnoticed until something compares raw score
magnitudes or switches to an inner-product operator. Normalising here costs
nothing and removes the trap.

**Query and document embeddings are not the same operation.** `task_type`
changes the vector. Embedding a query with `RETRIEVAL_DOCUMENT` degrades recall
quietly, which is why the query path is a separate function rather than a
default argument someone will forget to pass.

There is also nothing to meter: the endpoint returns no usage metadata at all.
Cost here is *estimated* from characters, and every name that carries it says so.
"""

from __future__ import annotations

import math
from typing import Any, Protocol

import psycopg

from app.extraction.llm import call_with_retry

DOCUMENT_TASK = "RETRIEVAL_DOCUMENT"
QUERY_TASK = "RETRIEVAL_QUERY"

BATCH_SIZE = 32
"""Chunks per request. Small enough to stay well inside the endpoint's
per-request input cap, large enough that a few thousand chunks is a couple of
minutes rather than an afternoon."""

CHARS_PER_TOKEN = 4
"""Rough conversion for the cost estimate. English prose runs about four
characters per token; this is an estimate and is never presented as anything
else."""


class EmbeddingsResource(Protocol):
    def embed_content(self, **kwargs: Any) -> Any: ...


class GenaiLike(Protocol):
    """Just enough SDK surface to allow a fake in tests."""

    @property
    def models(self) -> EmbeddingsResource: ...


class EmbeddingError(RuntimeError):
    """The embedding call returned something unusable."""


def normalise(values: list[float]) -> list[float]:
    """Scale to unit length. A zero vector is returned unchanged."""
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        return values
    return [v / norm for v in values]


def _check_alignment(texts: list[str], vectors: list[Any], model: str) -> None:
    """One vector per input, or nothing.

    The failure this prevents is not a crash. A short batch zipped against its
    inputs assigns each embedding to the wrong chunk, and the only symptom is
    retrieval that returns plausible-looking irrelevant results forever.
    """
    if len(vectors) != len(texts):
        raise EmbeddingError(
            f"{model} returned {len(vectors)} embeddings for {len(texts)} inputs. "
            "The batch is misaligned; refusing to guess which vector belongs to which chunk."
        )


def _embed_batch(
    client: GenaiLike,
    texts: list[str],
    *,
    model: str,
    dimensions: int,
    task_type: str,
) -> list[list[float]]:
    from google.genai import types

    config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=dimensions)
    # One Content per text. See the module docstring: bare strings are coerced
    # differently by different models, and one of those ways is silently wrong.
    contents = [types.Content(parts=[types.Part(text=text)]) for text in texts]

    response = call_with_retry(
        lambda: client.models.embed_content(model=model, contents=contents, config=config),
        what=model,
    )

    embeddings = list(getattr(response, "embeddings", None) or [])
    _check_alignment(texts, embeddings, model)

    vectors: list[list[float]] = []
    for embedding in embeddings:
        values = list(getattr(embedding, "values", None) or [])
        if len(values) != dimensions:
            raise EmbeddingError(
                f"{model} returned {len(values)} dimensions, expected {dimensions}"
            )
        vectors.append(normalise(values))

    return vectors


def embed_documents(
    client: GenaiLike,
    texts: list[str],
    *,
    model: str,
    dimensions: int,
    batch_size: int = BATCH_SIZE,
) -> tuple[list[list[float]], int]:
    """Embed chunks for storage. Returns `(vectors, request_count)`."""
    vectors: list[list[float]] = []
    calls = 0

    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(
            _embed_batch(client, batch, model=model, dimensions=dimensions, task_type=DOCUMENT_TASK)
        )
        calls += 1

    return vectors, calls


def embed_query(client: GenaiLike, text: str, *, model: str, dimensions: int) -> list[float]:
    """Embed a search query. Note the task type differs from the document path."""
    return _embed_batch(client, [text], model=model, dimensions=dimensions, task_type=QUERY_TASK)[0]


def estimated_tokens(texts: list[str]) -> int:
    return sum(len(text) for text in texts) // CHARS_PER_TOKEN


def to_pgvector(values: list[float]) -> str:
    """pgvector's text input form.

    Sent as text and cast in SQL rather than pulling in `pgvector[psycopg]` for
    one conversion. Nothing here ever reads a vector back out, which is the only
    part of that package that would actually earn its place.
    """
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"


def column_dimensions(conn: psycopg.Connection) -> int:
    """The width `chunks.embedding` was actually created with.

    pgvector stores the declared dimension in the column's typmod. Checked at
    the start of ingestion because a configured width that disagrees with the
    column does not fail on insert -- Postgres rejects it, loudly, but only
    after the embeddings have been paid for.
    """
    row = conn.execute(
        """
        SELECT atttypmod
          FROM pg_attribute
         WHERE attrelid = 'chunks'::regclass
           AND attname = 'embedding'
        """
    ).fetchone()
    if row is None:
        raise EmbeddingError("chunks.embedding not found -- run migrations first")
    return int(row[0])
