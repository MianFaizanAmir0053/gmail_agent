# M12 · Retrieval evaluation

**Est.** 1 day · **Depends on** M11 · **Blocks** M13

## Goal

Prove — or disprove — that hybrid retrieval helped. Don't claim an improvement you haven't measured.

## Deliverables

- `data/retrieval_eval.json` — 20–30 labeled queries with known-relevant chunk IDs
- `app/eval/retrieval.py` — recall@k and MRR
- `results/retrieval-comparison.md` — the committed results table

## Metrics

- **Recall@k** (k = 3, 5, 10) — did the relevant chunk make the cut?
- **MRR** — how high did it rank?

Recall@k matters more here. The agent reads the top few results; a relevant chunk at position 9 is functionally invisible to it.

## The comparison

Run all four configurations against the same query set:

| Config | Recall@5 | MRR |
|---|---|---|
| Vector only | | |
| BM25 only | | |
| RRF fusion | | |
| RRF + rerank | | |

## Building the query set

Derive it from real needs, not invented ones:

- "What did we agree with <person> about <project>?"
- "When did I last meet <person>?"
- "What's <person>'s email address?"
- Queries with exact identifiers (ticket numbers, project codenames) — where BM25 should beat vector search
- Paraphrased/semantic queries — where vector search should win

Label by hand. Yes, it's tedious. It's also the only reason the numbers mean anything.

## Report honestly

**If RRF beats RRF+rerank, publish that.** It's the more interesting result and the more credible one — it says you measured rather than cargo-culted. Plenty of production systems skip reranking for exactly this reason.

The failure mode to avoid is adding a component because it's on a list of things RAG systems have, then asserting it improved things.

## Exit criterion

The comparison table is committed with real numbers, and the M11 reranker flag is set to whatever the data actually supports.

## Running notes

**The result is negative, and it is the most useful thing in the module.**
Hybrid retrieval did not beat vector search — it lost 7 points of hit@5. The
full table and reasoning are in [`results/retrieval-comparison.md`](../../results/retrieval-comparison.md).

| Config | hit@5 | MRR |
|---|---|---|
| **vector only** | **100%** | **0.951** |
| keyword only | 83% | 0.791 |
| RRF fusion | 93% | 0.898 |
| RRF 2:1 | 97% | 0.900 |

**The keyword half lost the category it was built for.** Nine of the 29 queries
were bare identifiers — `ORD-20260814-4WJWF`, `pull request 183` — chosen
specifically so BM25 would win. It scored 100% on them. So did vector search, at
rank 1, against dozens of near-identical emails differing only in that code.
`gemini-embedding-001` encodes rare tokens far better than the usual "embeddings
can't do exact match" advice assumes.

**Fusion had nothing to add, so it could only displace.** Vector missed zero
queries at k=5. Any fusion partner can therefore only push correct results down.
This is worth stating as a general rule: RRF is a bet that two retrievers fail on
different queries, and when one of them never fails, the bet has no upside.

### A bug that would have made the table a lie

The first run scored keyword search at 34%. That looked like a plausible story
about BM25 being weak on natural language, and it was wrong.
`plainto_tsquery` **ANDs** its terms — `who is Alice from Zetafonts` becomes
`alic & zetafont & want`, so a chunk had to contain all three stems or it did not
match at all. The keyword half had silently been working only on bare
identifiers, in production as well as in the eval. Rewriting the query as an OR
through `websearch_to_tsquery` moved it from 34% to 83%.

Publishing the first table would have produced a *more* flattering conclusion for
the design that shipped in M11 — hybrid clearly beating the useless keyword half.
The correct number is the one that undermines it.

### Weighted RRF is not a middle ground

Added to test whether RRF's loss was inherent or just the cost of treating a
strong and a weak retriever as equals. The weighted variants scored close to
vector-only because they nearly *are* vector-only: with `k = 60` and 20
candidates per search, a keyword-only document scores at most `w_k / 61` while
the worst vector candidate scores `w_v / 80`, so the keyword half stops
contributing anything of its own once `w_v / w_k` exceeds about 1.3. At 2:1 it
only reorders. That is why 2:1 and 4:1 produce identical hit rates.

`HYBRID_WEIGHTS` is consequently unweighted. A knob that silently disables the
component it is supposed to balance is worse than no knob.

### The reranker: not run, for a stated reason

Vector-only reaches 100% hit@5. Reranking reorders a candidate list; it cannot
add a document retrieval never returned. There is no headroom at the depth the
agent reads, so a reranker cannot help — that is a measurement, not a preference,
and it is the answer to "set the flag to whatever the data supports". The seam
stays wired and unused.

### Method notes

- Labels are Gmail message ids, not chunk ids. Chunk ids are `BIGSERIAL` and
  would silently point at nothing after a re-ingest.
- `check_labels` fails the run if a labelled message is absent from the corpus.
  A typo'd id otherwise drags every configuration down equally and looks like
  retrieval being uniformly worse rather than like a broken label.
- Queries are embedded once and reused across configurations, so the modes differ
  only by retrieval strategy.
- The query set is gitignored — real names, real message ids, same rule as M02's
  raw emails. `data/retrieval_eval.example.json` documents the shape.
- **n = 29.** The vector/RRF gap is two queries. The defensible claim is "the
  data does not support paying for two searches", not "fusion is bad".

### Corpus note

The corpus was grown from 29 chunks to 343 before measuring. At 29 chunks, five
results is a sixth of everything and every configuration scores near 100% —
the eval would have measured nothing.

That backfill also found two ingestion faults: the free-tier embed quota is
counted **per document, not per request** (so batching buys round trips and no
throughput, and 300 messages hit the wall), and the whole run was one
transaction, so the failure discarded every embedding it had already paid for.
Fixed with a sliding-window pacer and a commit per batch.

### Verified

```
ruff / mypy --strict   clean
pytest                 421 passed
```
