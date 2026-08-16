# M11 · Hybrid retrieval & the `search_context` tool

**Est.** 2 days · **Depends on** M10 · **Blocks** M12, M13

## Goal

Give the agent a retrieval tool that beats naive vector search — and be able to prove the "beats" part in M12.

## Deliverables

- `app/rag/search.py` — vector search, BM25 search, RRF fusion
- `app/tools/search_context.py` — the model-callable tool
- Optional reranker behind a feature flag
- Wired into the M05 graph so the extractor can call it

## Hybrid search

Vector search finds semantic matches; BM25 finds exact terms (names, project codes, ticket IDs) that embeddings routinely miss. Fuse with **Reciprocal Rank Fusion** — no score normalisation needed, which is exactly why it's the right default:

```python
def rrf(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores
```

## Reranking — mind the cost

A local cross-encoder (`bge-reranker` and friends) needs RAM you will not have on a $5 instance. Two viable options:

- A hosted rerank API (Cohere, Voyage) — costs money per call
- **Skip it and rely on RRF** — then measure in M12 whether it would have helped

Put it behind a flag either way so M12 can A/B it. "I tried reranking and it didn't beat RRF on my eval set, so I didn't ship it" is a better answer than a checkbox in a feature list.

## The tool

```python
{
    "name": "search_context",
    "description": (
        "Search past email threads for context about people, projects, or prior "
        "commitments. Call this when an email references a person, meeting, or "
        "decision you don't have details for — e.g. to resolve an attendee's "
        "full email address, or to check what was agreed previously."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "participant": {"type": "string", "description": "Optional: filter by participant email"},
            "since": {"type": "string", "format": "date", "description": "Optional: only threads after this date"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "strict": True,
}
```

Be **prescriptive about when to call it** in the description, not just what it does. Recent models are conservative about reaching for tools; a description that names the trigger condition measurably raises the call rate.

## Exit criterion

The extractor can correctly resolve an attendee identity that it demonstrably could not before. Write that specific case down as a before/after — it's the concrete example for the README.

## Running notes

_(record what surprised you here)_
