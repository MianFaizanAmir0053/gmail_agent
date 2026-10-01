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

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Protocol

import psycopg

from app.policy.budget import (
    BudgetExhaustedError,
    Gate,
    MessageTooCostlyError,
    UnpricedModelError,
)
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


_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def or_terms(query: str) -> str:
    """Rewrite free text as an OR query for `websearch_to_tsquery`.

    **`plainto_tsquery` ANDs its terms**, which is the single most damaging
    default in this file. "who is Alice from Zetafonts and what does she want"
    becomes `alic & zetafont & want`, so a chunk has to contain all three or it
    does not match *at all* -- and the keyword half silently degrades to working
    only on bare identifiers. Measured at 34% hit@5 before this, 66% after.

    OR does not make ranking indiscriminate: `ts_rank` still rewards documents
    that match more of the terms, which is the behaviour wanted from the keyword
    half in the first place.

    `websearch_to_tsquery` rather than `to_tsquery` because it never raises. The
    query text is written by a language model and will contain punctuation,
    quotes, and the occasional stray operator.
    """
    return " or ".join(_WORD.findall(query))


def keyword_search(
    conn: psycopg.Connection,
    query: str,
    *,
    limit: int = CANDIDATES_PER_SEARCH,
    participant: str | None = None,
    since: date | None = None,
) -> list[Hit]:
    """BM25-style ranking over the generated `tsv` column."""
    terms = or_terms(query)
    if not terms:
        return []

    rows = conn.execute(
        f"""
        {_SELECT}, ts_rank(tsv, q) AS score
          FROM chunks, websearch_to_tsquery('english', %(query)s) AS q
         WHERE tsv @@ q
        {_FILTERS}
         ORDER BY score DESC
         LIMIT %(limit)s
        """,
        {"query": terms, "participant": participant, "since": since, "limit": limit},
    ).fetchall()
    return [_to_hit(row, float(row[7])) for row in rows]


def rrf(
    rankings: list[list[int]], k: int = RRF_K, weights: Sequence[float] | None = None
) -> dict[int, float]:
    """Reciprocal Rank Fusion. Ranks are 1-based.

    `weights` is available but is not the default, and using it should feel like
    a cost: an unweighted RRF has nothing to tune, and the moment one list is
    worth twice another, that ratio is a number fitted to a particular corpus
    and a particular embedding model. M12 measures what it actually buys.
    """
    factors = list(weights) if weights is not None else [1.0] * len(rankings)
    scores: dict[int, float] = {}
    for ranking, weight in zip(rankings, factors, strict=True):
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
    return scores


def fuse(
    *result_lists: list[Hit],
    limit: int = DEFAULT_LIMIT,
    k: int = RRF_K,
    weights: Sequence[float] | None = None,
) -> list[Hit]:
    """Merge ranked lists by RRF, keeping one Hit per chunk."""
    by_id: dict[int, Hit] = {}
    for results in result_lists:
        for hit in results:
            by_id.setdefault(hit.chunk_id, hit)

    scores = rrf(
        [[hit.chunk_id for hit in results] for results in result_lists], k=k, weights=weights
    )
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))

    # The fused score replaces the per-search one, which was measured on a scale
    # that no longer means anything once two of them have been combined.
    return [replace(by_id[chunk_id], score=score) for chunk_id, score in ordered[:limit]]


HYBRID_WEIGHTS: Sequence[float] | None = None
"""Unweighted, deliberately, despite M12 scoring the weighted variants higher.

Weighting turns out not to be a middle ground. With `k = 60` and 20 candidates
per search, a document only the keyword half found scores at most `w_k / 61`,
while the *worst* vector candidate scores `w_v / 80`. So the keyword half can
contribute a result of its own only while `w_v / w_k < 80/61`, about 1.3 -- at
2:1 every vector candidate already outranks every keyword-only one, and fusion
degenerates into vector search reordered by keyword agreement.

That is exactly why `rrf-2:1` and `rrf-4:1` scored alike and close to vector
alone in `results/retrieval-comparison.md`. They were not a compromise between
the two halves; they were vector-only wearing a costume. If fusion is switched
on it should be the real thing.
"""


@dataclass(slots=True)
class ContextSearch:
    """Everything `search_context` needs, injected so tests can fake it."""

    conn: psycopg.Connection
    client: EmbeddingClientLike
    embedding_model: str
    embedding_dimensions: int
    mode: str = "vector"
    """`vector` or `hybrid`.

    Defaults to `vector` because that is what M12 measured, not because fusion
    is uninteresting. On this corpus the keyword half never returned a relevant
    message that vector search had missed -- including on bare order references,
    where it was supposed to win outright -- so fusion could only displace
    correct results, and it did: hit@5 fell from 100% to 93%.

    See `results/retrieval-comparison.md`. It is a setting rather than a deletion
    because that is a result about one mailbox and one embedding model, and the
    argument for keeping a lexical half is about the corpora it was not measured
    on.
    """
    rerank: Reranker | None = None

    def __call__(
        self,
        query: str,
        *,
        participant: str | None = None,
        since: date | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[Hit]:
        try:
            embedded = embed_query(
                self.client,
                query,
                model=self.embedding_model,
                dimensions=self.embedding_dimensions,
            )
        except (BudgetExhaustedError, UnpricedModelError, MessageTooCostlyError):
            # The spend gate said no (M17, D5): stopping is the point, so the
            # refusal goes up to whoever stops the message.
            raise
        except Exception:
            # Degrade to keyword-only rather than failing the extraction. Half a
            # search is worth more here than an exception thrown three nodes deep
            # over a lookup the model asked for speculatively. This is also the
            # standing argument for keeping the lexical half maintained even
            # while it is switched off.
            hits = keyword_search(self.conn, query, participant=participant, since=since)[:limit]
        else:
            hits = self._search(query, embedded, participant=participant, since=since, limit=limit)

        return self.rerank(query, hits) if self.rerank else hits

    def _search(
        self,
        query: str,
        embedded: list[float],
        *,
        participant: str | None,
        since: date | None,
        limit: int,
    ) -> list[Hit]:
        if self.mode == "vector":
            return vector_search(
                self.conn, embedded, limit=limit, participant=participant, since=since
            )

        return fuse(
            vector_search(self.conn, embedded, participant=participant, since=since),
            keyword_search(self.conn, query, participant=participant, since=since),
            limit=limit,
            weights=HYBRID_WEIGHTS,
        )


def build_context_search(
    conn: psycopg.Connection, settings: Any, *, gate: Gate | None = None
) -> ContextSearch:
    """Wire a searcher from settings, metered like the pipeline (M17, D5): by
    `gate`, or one on this connection."""
    from app.policy import models

    return ContextSearch(
        conn=conn,
        client=models.client(settings, gate or models.gate(settings, conn)),
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        mode=settings.retrieval_mode,
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
