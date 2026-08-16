# M10 · RAG ingestion pipeline

**Est.** 3 days · **Depends on** M08 · **Blocks** M11

## Goal

Past email threads become searchable context, so the agent can answer "who is Ahmed and what did we agree last time?" before drafting an event.

## Deliverables

- `migrations/004_pgvector.sql` — extension, `chunks` table, HNSW index
- `app/rag/clean.py` — quoted-reply / signature / HTML stripping
- `app/rag/chunk.py` — chunking with metadata
- `app/rag/embed.py` — batch embedding
- `app/rag/ingest.py` — the full pipeline with content-hash dedupe
- Run stats written to the M08 tables

## Schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE chunks (
    id           BIGSERIAL PRIMARY KEY,
    thread_id    TEXT NOT NULL,
    message_id   TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,     -- idempotency
    participants TEXT[] NOT NULL,
    sent_at      TIMESTAMPTZ NOT NULL,
    embedding    vector(1024),
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON chunks USING gin (tsv);
```

The `tsv` column is generated now so M11's BM25 half needs no migration.

## Cleaning is most of the work

Raw email is mostly noise. Strip, in order:

1. Quoted replies (`> ` prefixes, `On <date>, <person> wrote:`, Gmail's `gmail_quote` div)
2. Signatures (`--` delimiter, and the trailing contact-details block)
3. HTML → text, preserving paragraph breaks
4. Legal footers and unsubscribe boilerplate

Skipping this means your embeddings encode the same signature block a thousand times and every retrieval returns near-duplicates. Budget real time here — it is the difference between retrieval that works and retrieval that looks like it works.

## Chunking

Thread-aware, not fixed-size. A short email is one chunk. A long one splits on paragraph boundaries with overlap. Every chunk carries `thread_id`, `participants`, and `sent_at` — M11 needs them for filtering, and the agent needs them to answer "when did we last talk about this?"

## Idempotency

`content_hash = sha256(cleaned_text)` with a UNIQUE constraint. Upsert with `ON CONFLICT DO NOTHING`. Re-running ingestion over the same mailbox must insert zero rows — same principle as M04, different table.

## HNSW index note

Building the HNSW index needs working memory. On a small instance, build it **after** the initial bulk load rather than before, and consider raising `maintenance_work_mem` for the duration.

## Exit criterion

Run ingestion twice over the same mailbox: **zero new rows on the second run.** Then spot-check 10 chunks by hand — is the text actually clean, or is it signature soup?

## Running notes

_(record what surprised you here)_
