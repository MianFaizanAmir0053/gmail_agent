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

_(record what surprised you here)_
