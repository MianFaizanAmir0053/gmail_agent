"""Hybrid retrieval over the chunk corpus.

Two searches, fused. They fail in different directions, which is the entire
argument for running both:

- **Vector search** matches meaning. It finds "can we push the review?" from a
  query about rescheduling, and it is hopeless at exact strings -- a ticket ID
  or an unusual surname embeds to roughly nothing.
- **Keyword search** matches terms. It finds `ORD-20260814-4WJWF` and `Ahmed`
  precisely, and returns nothing at all when the query and the document happen
  to use different words for the same thing.

Fusion is **Reciprocal Rank Fusion**, which combines *ranks* rather than scores.
That is the point: a cosine similarity of 0.71 and a `ts_rank` of 0.09 are not
on any common scale, and every attempt to normalise them into one introduces a
tuning parameter that has to be re-tuned whenever the corpus changes. RRF has
one constant, it is famously insensitive to it, and it needs to know nothing
about either scoring system.

Reranking is left as a seam rather than an implementation. A local cross-encoder
needs RAM a five-dollar instance does not have, and a hosted reranker is another
paid dependency; M12 measures whether RRF is already enough before either is
worth buying. `rerank` accepts a callable so that experiment is a parameter, not
a rewrite.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Protocol

import psycopg

from app.rag.embed import embed_query, to_pgvector

RRF_K = 60
"""The rank-fusion constant, at its standard value.

It damps the contribution of top ranks so that one search cannot dominate the
other. Results are famously insensitive to it, which is most of why RRF is a
good default: there is nothing here to tune per corpus.
"""

DEFAULT_LIMIT = 5
CANDIDATES_PER_SEARCH = 20
"""How deep each search goes before fusion.

Deeper than the final limit on purpose: a document ranked eighth by vector and
ninth by keyword is exactly the kind of consensus result RRF exists to surface,
and it is invisible if both lists are cut at five.
"""

SNIPPET_CHARS = 400


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: int
    thread_id: str
    message_id: str
    subject: str
    content: str
    participants: tuple[str, ...]
    sent_at: datetime
    score: float

    def snippet(self, limit: int = SNIPPET_CHARS) -> str:
        if len(self.content) <= limit:
            return self.content
        return self.content[:limit].rsplit(" ", 1)[0] + "…"


Reranker = Callable[[str, list[Hit]], list[Hit]]
"""The seam an M12 experiment plugs into. Takes the query and fused hits,
returns them reordered."""


class EmbeddingClientLike(Protocol):
    @property
    def models(self) -> Any: ...


_SELECT = """
    SELECT id, thread_id, message_id, subject, content, participants, sent_at
"""

_FILTERS = """
      AND (%(participant)s::text IS NULL OR %(participant)s = ANY(participants))
      AND (%(since)s::date IS NULL OR sent_at >= %(since)s::date)
"""


def _to_hit(row: Sequence[Any], score: float) -> Hit:
    return Hit(
        chunk_id=row[0],
        thread_id=row[1],
        message_id=row[2],
        subject=row[3],
        content=row[4],
        participants=tuple(row[5]),
        sent_at=row[6],
        score=score,
    )


def vector_search(
    conn: psycopg.Connection,
    query_vector: list[float],
    *,
    limit: int = CANDIDATES_PER_SEARCH,
    participant: str | None = None,
    since: date | None = None,
) -> list[Hit]:
    rows = conn.execute(
        f"""
        {_SELECT}, 1 - (embedding <=> %(vec)s::vector) AS score
          FROM chunks
         WHERE embedding IS NOT NULL
        {_FILTERS}
         ORDER BY embedding <=> %(vec)s::vector
         LIMIT %(limit)s
        """,
        {
            "vec": to_pgvector(query_vector),
            "participant": participant,
            "since": since,
            "limit": limit,
        },
    ).fetchall()
    return [_to_hit(row, float(row[7])) for row in rows]


def keyword_search(
    conn: psycopg.Connection,
    query: str,
    *,
    limit: int = CANDIDATES_PER_SEARCH,
    participant: str | None = None,
    since: date | None = None,
) -> list[Hit]:
    """BM25-style ranking over the generated `tsv` column.

    `plainto_tsquery` rather than `to_tsquery`: the query text comes from a
    language model and will contain punctuation, quotes, and the occasional
    stray operator, all of which make `to_tsquery` raise rather than return
    nothing useful.
    """
    rows = conn.execute(
        f"""
        {_SELECT}, ts_rank(tsv, q) AS score
          FROM chunks, plainto_tsquery('english', %(query)s) AS q
         WHERE tsv @@ q
        {_FILTERS}
         ORDER BY score DESC
         LIMIT %(limit)s
        """,
        {"query": query, "participant": participant, "since": since, "limit": limit},
    ).fetchall()
    return [_to_hit(row, float(row[7])) for row in rows]


def rrf(rankings: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal Rank Fusion. Ranks are 1-based."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def fuse(*result_lists: list[Hit], limit: int = DEFAULT_LIMIT, k: int = RRF_K) -> list[Hit]:
    """Merge ranked lists by RRF, keeping one Hit per chunk."""
    by_id: dict[int, Hit] = {}
    for results in result_lists:
        for hit in results:
            by_id.setdefault(hit.chunk_id, hit)

    scores = rrf([[hit.chunk_id for hit in results] for results in result_lists], k=k)
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))

    # The fused score replaces the per-search one, which was measured on a scale
    # that no longer means anything once two of them have been combined.
    return [replace(by_id[chunk_id], score=score) for chunk_id, score in ordered[:limit]]


@dataclass(slots=True)
class ContextSearch:
    """Everything `search_context` needs, injected so tests can fake it."""

    conn: psycopg.Connection
    client: EmbeddingClientLike
    embedding_model: str
    embedding_dimensions: int
    rerank: Reranker | None = None

    def __call__(
        self,
        query: str,
        *,
        participant: str | None = None,
        since: date | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[Hit]:
        keyword = keyword_search(self.conn, query, participant=participant, since=since)

        vector: list[Hit] = []
        try:
            embedded = embed_query(
                self.client,
                query,
                model=self.embedding_model,
                dimensions=self.embedding_dimensions,
            )
        except Exception:
            # Degrade to keyword-only rather than failing the extraction. Half a
            # search is worth more here than an exception thrown three nodes deep
            # over a lookup the model asked for speculatively.
            pass
        else:
            vector = vector_search(self.conn, embedded, participant=participant, since=since)

        fused = fuse(vector, keyword, limit=limit)
        return self.rerank(query, fused) if self.rerank else fused


def build_context_search(conn: psycopg.Connection, settings: Any) -> ContextSearch:
    """Wire a searcher from settings. Imports the SDK lazily, like the pipeline."""
    from google import genai

    return ContextSearch(
        conn=conn,
        client=genai.Client(api_key=settings.gemini_api_key.get_secret_value()),
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
    )


def main() -> None:
    """Search the corpus by hand.

        python -m app.rag.search "who is ahmed"

    Exists so retrieval quality can be judged directly, without inferring it
    from whatever the extractor happened to produce.
    """
    import argparse

    from app.config import get_settings
    from app.store.db import connect

    parser = argparse.ArgumentParser(description="Search ingested email context.")
    parser.add_argument("query")
    parser.add_argument("--participant", default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--mode", choices=("hybrid", "vector", "keyword"), default="hybrid")
    args = parser.parse_args()

    settings = get_settings()

    with connect(settings.database_url) as conn:
        search = build_context_search(conn, settings)

        if args.mode == "keyword":
            hits = keyword_search(conn, args.query, limit=args.limit, participant=args.participant)
        elif args.mode == "vector":
            embedded = embed_query(
                search.client,
                args.query,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
            )
            hits = vector_search(conn, embedded, limit=args.limit, participant=args.participant)
        else:
            hits = search(args.query, participant=args.participant, limit=args.limit)

        if not hits:
            print("No matches.")
            return

        for hit in hits:
            print(f"\n{hit.score:.4f}  {hit.sent_at:%Y-%m-%d}  {hit.subject[:70]}")
            print(f"        {', '.join(hit.participants) or '(none)'}")
            print(f"        {hit.snippet(200)}")


if __name__ == "__main__":
    main()
