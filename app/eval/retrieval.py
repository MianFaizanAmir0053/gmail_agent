"""Retrieval evaluation: does hybrid search actually beat either half?

    python -m app.eval.retrieval
    python -m app.eval.retrieval --modes vector keyword rrf

Labels are `message_id`s, not chunk ids. Chunk ids are `BIGSERIAL` and change
every time the corpus is rebuilt, so a query set keyed on them would silently
become a set of labels for rows that no longer exist. Gmail message ids are
stable, and "the answer is somewhere in that message" is the claim being made
anyway.

## Two metrics, not one wearing two names

**hit@k** -- did *any* relevant message reach the top k. This is the one that
matches how the system actually works: the agent reads a handful of results and
needs an answer to be among them. A relevant chunk at rank nine is invisible to
it whether or not it technically exists.

**recall@k** -- what *proportion* of the relevant messages reached the top k.
Lower by construction whenever a query has several right answers.

They are reported separately because calling hit-rate "recall" is the kind of
small dishonesty that makes every other number in a table suspect.

**MRR** -- one over the rank of the first relevant message, averaged. It is what
separates two systems that both find the answer but disagree about how obvious
it is.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from pydantic import BaseModel, Field

from app.rag.search import (
    CANDIDATES_PER_SEARCH,
    Hit,
    Reranker,
    fuse,
    keyword_search,
    vector_search,
)

DATASET = Path("data/retrieval_eval.json")
KS = (3, 5, 10)
DEPTH = 10
"""How far down the ranking the metrics look. Nothing below this is reachable
by the agent, so measuring it would flatter the numbers without changing them."""

FUSION_WEIGHTS: dict[str, Sequence[float] | None] = {
    "rrf": None,
    "rrf-2:1": (2.0, 1.0),
    "rrf-4:1": (4.0, 1.0),
}
"""Fusion configurations, vector weight first.

The weighted variants exist to answer one question: when RRF scores below the
better of its two inputs, is that inherent to rank fusion or just the cost of
treating a strong retriever and a weak one as equals? Weighting is not free --
it is exactly the tuned constant unweighted RRF exists to avoid -- so it has to
earn its place with a number.
"""

MODES = ("vector", "keyword", "rrf", "rrf-2:1", "rrf-4:1")


class RetrievalCase(BaseModel):
    query: str
    relevant: list[str] = Field(min_length=1)
    kind: str = "semantic"
    """`exact` (an identifier BM25 should nail), `identity` (who is this person),
    or `semantic` (a paraphrase vector search should reach). Grouping by kind is
    the only way to see *where* each half earns its keep, rather than just that
    the average moved."""
    note: str = ""


def load_cases(path: Path = DATASET) -> list[RetrievalCase]:
    if not path.exists():
        raise SystemExit(
            f"No query set at {path}. It is gitignored: it names real people and "
            "real message ids. See data/retrieval_eval.example.json for the shape."
        )
    raw: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return [RetrievalCase.model_validate(case) for case in raw]


def check_labels(conn: psycopg.Connection, cases: list[RetrievalCase]) -> None:
    """Every labelled message must exist in the corpus.

    A mistyped id is invisible otherwise: it simply never matches, so it drags
    every configuration down by the same amount and looks like retrieval being
    uniformly worse rather than like a broken label.
    """
    labelled = sorted({message_id for case in cases for message_id in case.relevant})
    rows = conn.execute(
        "SELECT DISTINCT message_id FROM chunks WHERE message_id = ANY(%s)", (labelled,)
    ).fetchall()
    missing = set(labelled) - {row[0] for row in rows}
    if missing:
        raise SystemExit(
            f"{len(missing)} labelled message(s) are not in the corpus: {sorted(missing)}\n"
            "Either the labels are wrong or ingestion has not covered them."
        )


def ranked_messages(hits: Sequence[Hit]) -> list[str]:
    """Chunk ranking to message ranking, first occurrence wins.

    Two chunks of the same message are one answer, not two. Left as-is they
    would push distinct results out of the top k and quietly inflate MRR.
    """
    seen: dict[str, None] = {}
    for hit in hits:
        seen.setdefault(hit.message_id, None)
    return list(seen)


@dataclass(slots=True)
class Scores:
    hits: dict[int, float] = field(default_factory=dict)
    recall: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    queries: int = 0


def score(rankings: list[tuple[RetrievalCase, list[str]]], ks: Sequence[int] = KS) -> Scores:
    result = Scores(queries=len(rankings))
    if not rankings:
        return result

    for k in ks:
        found = 0
        recalled = 0.0
        for case, ranking in rankings:
            top = set(ranking[:k])
            relevant = set(case.relevant)
            if top & relevant:
                found += 1
            recalled += len(top & relevant) / len(relevant)
        result.hits[k] = found / len(rankings)
        result.recall[k] = recalled / len(rankings)

    reciprocal = 0.0
    for case, ranking in rankings:
        for index, message_id in enumerate(ranking[:DEPTH], start=1):
            if message_id in case.relevant:
                reciprocal += 1.0 / index
                break
    result.mrr = reciprocal / len(rankings)
    return result


def retrieve(
    conn: psycopg.Connection,
    case: RetrievalCase,
    vector: list[float] | None,
    *,
    mode: str,
    rerank: Reranker | None = None,
) -> list[str]:
    if mode == "keyword":
        return ranked_messages(keyword_search(conn, case.query, limit=DEPTH))

    if vector is None:
        # Never silently: a mode that needs embeddings and did not get them
        # would otherwise score zero and look like a bad retriever.
        raise ValueError(f"mode {mode!r} needs a query embedding")

    if mode == "vector":
        return ranked_messages(vector_search(conn, vector, limit=DEPTH))

    if mode in FUSION_WEIGHTS:
        fused = fuse(
            vector_search(conn, vector, limit=CANDIDATES_PER_SEARCH),
            keyword_search(conn, case.query, limit=CANDIDATES_PER_SEARCH),
            limit=DEPTH,
            weights=FUSION_WEIGHTS[mode],
        )
        if rerank is not None:
            fused = rerank(case.query, fused)
        return ranked_messages(fused)

    raise ValueError(f"unknown mode {mode!r}")


def _table(results: dict[str, Scores]) -> str:
    lines = [
        "| Config | hit@3 | hit@5 | hit@10 | recall@5 | MRR |",
        "|---|---|---|---|---|---|",
    ]
    for name, scores in results.items():
        lines.append(
            f"| {name} | {scores.hits[3]:.0%} | {scores.hits[5]:.0%} | {scores.hits[10]:.0%} "
            f"| {scores.recall[5]:.0%} | {scores.mrr:.3f} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare retrieval configurations.")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--modes", nargs="+", default=list(MODES))
    parser.add_argument("--by-kind", action="store_true", help="Break results down by query kind.")
    args = parser.parse_args()

    from app.config import get_settings
    from app.rag.embed import embed_query
    from app.rag.search import build_context_search
    from app.store.db import connect

    settings = get_settings()
    cases = load_cases(args.dataset)

    with connect(settings.database_url) as conn:
        check_labels(conn, cases)
        search = build_context_search(conn, settings)

        # Embedded once and reused across every mode. Re-embedding per mode
        # would triple the cost and, worse, let the configurations differ by
        # something other than the retrieval strategy.
        vectors = {
            case.query: embed_query(
                search.client,
                case.query,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
            )
            for case in cases
        }

        results: dict[str, Scores] = {}
        per_case: dict[str, list[tuple[RetrievalCase, list[str]]]] = {}

        for mode in args.modes:
            rankings = [
                (case, retrieve(conn, case, vectors[case.query], mode=mode)) for case in cases
            ]
            per_case[mode] = rankings
            results[mode] = score(rankings)

    print(f"\n{len(cases)} queries\n")
    print(_table(results))

    if args.by_kind:
        kinds = sorted({case.kind for case in cases})
        print("\nBy query kind (hit@5):\n")
        header = "| Config | " + " | ".join(
            f"{k} ({sum(c.kind == k for c in cases)})" for k in kinds
        )
        print(header + " |")
        print("|---" * (len(kinds) + 1) + "|")
        for mode in args.modes:
            cells = []
            for kind in kinds:
                subset = [pair for pair in per_case[mode] if pair[0].kind == kind]
                cells.append(f"{score(subset).hits[5]:.0%}")
            print(f"| {mode} | " + " | ".join(cells) + " |")

    print("\nMissed at 5, by mode:")
    for mode in args.modes:
        missed = [
            case.query
            for case, ranking in per_case[mode]
            if not set(ranking[:5]) & set(case.relevant)
        ]
        print(f"  {mode}: {len(missed)}")
        for query in missed:
            print(f"    - {query}")


if __name__ == "__main__":
    main()
