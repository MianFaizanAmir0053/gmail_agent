# Retrieval comparison

Measured 2026-08-17 against the live corpus: **343 chunks from 272 messages** of
a real personal mailbox, embedded with `gemini-embedding-001` at 1536 dimensions.

**29 hand-labelled queries.** Labels are Gmail message ids, not chunk ids —
chunk ids are `BIGSERIAL` and change whenever the corpus is rebuilt. The query
set itself is gitignored: it names real people and real message ids. See
`data/retrieval_eval.example.json` for the shape and `app/eval/retrieval.py` for
the harness.

Reproduce with:

```bash
python -m app.eval.retrieval --by-kind
```

## Metrics

**hit@k** — did *any* relevant message reach the top k. This is the number that
matters operationally: the agent reads a handful of results, so a relevant chunk
at rank nine is invisible to it.

**recall@k** — what *proportion* of relevant messages reached the top k. Lower by
construction on queries with several right answers.

They are reported separately because calling hit-rate "recall" is the kind of
small dishonesty that makes every other number in a table suspect.

## Results

| Config | hit@3 | hit@5 | hit@10 | recall@5 | MRR |
|---|---|---|---|---|---|
| **vector only** | **97%** | **100%** | **100%** | **95%** | **0.951** |
| keyword only (BM25) | 83% | 83% | 86% | 79% | 0.791 |
| RRF fusion | 90% | 93% | 100% | 90% | 0.898 |
| RRF weighted 2:1 | 90% | 97% | 100% | 91% | 0.900 |
| RRF weighted 4:1 | 90% | 97% | 100% | 91% | 0.895 |
| RRF + rerank | not run | | | | |

By query kind, hit@5:

| Config | exact (9) | identity (6) | semantic (14) |
|---|---|---|---|
| vector only | 100% | 100% | 100% |
| keyword only | 100% | 83% | 71% |
| RRF fusion | 100% | 100% | 86% |
| RRF weighted 2:1 | 100% | 100% | 93% |

## What this says

**Hybrid retrieval did not help. It hurt.** Vector search alone found a relevant
message in the top 5 for every query; fusion dropped that to 93%. The mechanism
is not subtle: the keyword half never returned a relevant message that vector
search had missed, so fusion had nothing to add and could only displace correct
results with wrong ones.

**BM25 lost the one category it was supposed to win.** Nine queries used bare
order references and PR numbers — `ORD-20260814-4WJWF`, `pull request 183` —
against a corpus containing dozens of near-identical emails differing only in
that code. Keyword search scored 100% on them. So did vector search, at rank 1,
with a similarity of 0.68. The received wisdom that embeddings cannot handle
exact identifiers does not hold for this model.

**The weighted variants are not a compromise.** They score close to vector-only
because they very nearly *are* vector-only. With `k = 60` and 20 candidates per
search, a keyword-only document scores at most `w_k / 61` while the worst vector
candidate scores `w_v / 80`, so the keyword half can contribute a result of its
own only while `w_v / w_k` stays under about 1.3. At 2:1 it contributes nothing
but reordering — which is why 2:1 and 4:1 land on the same hit rates.

**No reranker was run, and the reason is a number rather than a preference.**
Vector-only already reaches 100% hit@5. A reranker reorders a candidate list; it
cannot add a document that retrieval never returned. There is no headroom for it
to recover at the depth the agent actually reads. Buying either a cross-encoder's
RAM or a hosted rerank API to move a metric that is already at its ceiling would
be exactly the cargo-culting this evaluation exists to prevent.

## What was changed as a result

- `RETRIEVAL_MODE` now defaults to `vector`. Fusion stays available and tested.
- `HYBRID_WEIGHTS` is unweighted. If fusion is switched on it should be fusion,
  not vector search in a costume.
- The reranker seam in `app/rag/search.py` stays wired and unused.

## Caveats, stated rather than buried

**29 queries is a small n.** The gap between vector-only and RRF is two queries.
Treating that as a decisive victory would be overreading; the defensible claim is
narrower — the data does not support paying for two searches per lookup.

**The query set is mine, and I wrote it after seeing the corpus.** The exact
category was written specifically to favour BM25, which is the strongest form
this bias could take against the conclusion, and BM25 still did not win.

**The keyword half is kept for a reason the query set cannot measure.** It needs
no API call, so it remains the fallback when embedding fails — that path is live
and tested. Its value is insurance against a corpus or an embedding model that
behaves differently, which is a hypothesis, not a result.

**One bug found here changes how the earlier numbers should be read.** The first
run scored keyword search at 34% hit@5. That was not BM25 being weak; it was
`plainto_tsquery` ANDing every term, so `who is Alice from Zetafonts` required
one chunk to contain all three stems or it matched nothing at all. Rewriting the
query as an OR through `websearch_to_tsquery` took it from 34% to 83%. Every
keyword number above is post-fix.
