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

**Tools and structured output are not mutually exclusive on Gemini — verified,
not assumed.** The obvious fear was that adding `tools` would force giving up
`response_json_schema`, which would mean a searching extractor could no longer
return validated output. Probing the full loop showed otherwise: the model emits
a `function_call`, and the turn *after* the results are fed back still returns
schema-valid JSON. That single check is what made a real tool loop possible
instead of a two-call workaround where a separate "research" call feeds context
into a second structured one.

**Termination is a property of the request, not of the model.** On the final
allowed turn the tools are withdrawn from the config entirely, so the model has
nothing to call and must answer. Trusting a bound stated in a prompt would make
an unbounded cost a matter of luck.

**The no-tools path is byte-identical to what it was before.** `contents` is
still a bare string rather than a list, and `EXTRACT_SYSTEM` is unchanged — the
tool instructions live in a separate `SEARCH_SUFFIX` that is only appended when a
searcher is actually wired in. The frozen baseline was measured against that
exact request, and it should not move because an unrelated feature was added.

**Bad tool arguments are answered, not raised.** A hallucinated function name or
an unparseable date comes back as an `error` field the model reads on the next
turn. Raising would abort a whole extraction over a lookup the model made
speculatively, which is a far worse trade than one wasted turn.

**An empty result says so in words.** `{"results": []}` reads to a model like a
broken search and invites a retry; `"No past threads matched. Do not invent
details."` reads as evidence and closes the question.

**A failed embedding degrades to keyword-only** rather than failing the
extraction. Half a search three nodes deep beats an exception.

### RRF, and why not to normalise

Fusion combines *ranks*, never scores. A cosine similarity of 0.71 and a
`ts_rank` of 0.09 share no scale, and every scheme for normalising them into one
introduces a constant that needs re-tuning whenever the corpus changes. RRF has
one constant, is famously insensitive to it, and needs to know nothing about
either scoring system. Candidate depth is 20 per search rather than 5: a document
ranked eighth by vector and ninth by keyword is exactly the consensus result RRF
exists to surface, and it is invisible if both lists are cut at the final limit.

`plainto_tsquery`, not `to_tsquery` — the query text comes from a language model
and will contain punctuation and stray operators, which `to_tsquery` raises on.

### Reranking: a seam, not an implementation

`rerank` is a callable parameter, and it is wired and tested, but nothing is
plugged into it. A local cross-encoder needs RAM a five-dollar instance does not
have and a hosted reranker is another paid dependency — so M12 measures whether
RRF is already sufficient before either gets bought. A feature flag guarding an
implementation that does not exist would be decoration.

### Exit criterion — the before/after

`python -m app.rag.demo` runs the same extraction twice over the same text, once
with the tool withheld. Against the real ingested corpus, on an email that names
a person only by first name:

```
--- without search_context ---
  attendees   ['aarthi@elliottmoss.com']
  searches    0

--- with search_context ---
  attendees   ['aarthi@elliottmoss.com', 'molly.bhills@docorto.ai']
  searches    1
```

Same title, same date, same time. The address was resolved from a first name and
a company name, in one search. This is the case the extractor demonstrably could
not do before.

The email body is a CLI argument rather than a committed fixture: a convincing
example names a real person from a real mailbox, and a real address does not
belong in a public repository.

Run on `gemini-3.5-flash` rather than the configured extraction model, whose
free-tier allowance of **20 requests per day** was spent by then. Worth recording
as an operating condition rather than a footnote — it is the reason the two
stages are pinned to different models in the first place.

### Verified

```
ruff / mypy --strict   clean
pytest                 410 passed
```
