-- Retrieval corpus: cleaned email chunks, their embeddings, and a full-text
-- column for M11's BM25 half.
--
-- The vector width is 1536, not the 3072 the model returns by default, for a
-- hard reason: pgvector's `vector` type can only be HNSW-indexed up to 2000
-- dimensions. A 3072-wide column would need `halfvec` plus four times the
-- storage and index memory, on an instance sized for five dollars a month.
-- `gemini-embedding-001` is Matryoshka-trained, so a 1536 prefix is a real
-- embedding rather than a truncation that loses the tail of the meaning.
--
-- Change that number and every existing row becomes unqueryable against new
-- ones -- dimensions must match exactly to compare. `app.rag.embed` asserts the
-- configured width against this column at startup so the mismatch surfaces as
-- an error rather than as retrieval that is quietly wrong.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    id              BIGSERIAL PRIMARY KEY,
    thread_id       TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    ordinal         INT NOT NULL,

    subject         TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL,

    -- sha256 of (thread_id, content). Scoped to the thread rather than global:
    -- a bare global hash means two unrelated threads that both end in "Thanks!"
    -- collapse into one row, and the second thread silently loses a chunk.
    -- Within a thread, collapsing identical text is exactly what we want --
    -- it is quote leakage that survived cleaning.
    content_hash    TEXT NOT NULL UNIQUE,

    participants    TEXT[] NOT NULL DEFAULT '{}',
    sent_at         TIMESTAMPTZ NOT NULL,

    embedding       vector(1536),
    embedding_model TEXT,

    -- Generated now so M11's keyword half needs no migration. The subject is
    -- folded in because it routinely carries the terms someone would search
    -- for -- project names, ticket IDs -- that the body then refers to only as
    -- "it". Two-argument to_tsvector with a literal config is IMMUTABLE, which
    -- a generated column requires.
    tsv             tsvector GENERATED ALWAYS AS (
                        to_tsvector('english', coalesce(subject, '') || ' ' || content)
                    ) STORED,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chunks_message_ordinal_unique UNIQUE (message_id, ordinal)
);

CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_thread_idx ON chunks (thread_id);
CREATE INDEX IF NOT EXISTS chunks_sent_at_idx ON chunks (sent_at DESC);
CREATE INDEX IF NOT EXISTS chunks_participants_idx ON chunks USING gin (participants);

-- Cosine ops, because queries use `<=>`. An index built for one operator does
-- not serve another, and the planner silently falls back to a sequential scan
-- rather than telling you.
--
-- Created on an empty table, so it is built incrementally as rows arrive. At
-- ten thousand chunks or more the right move is the opposite: drop this, bulk
-- load, raise maintenance_work_mem, and rebuild once.
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- Ingestion stats.
--
-- Deliberately not the M08 `runs` table. That one is keyed on a Gmail message
-- ID with a NOT NULL constraint and a status set describing a graph run, so a
-- bulk backfill would have to invent both. Worse, the dashboard's cost view
-- divides spend by messages processed; folding a one-off backfill of a thousand
-- threads into it would wreck "cost per 100 emails", which is a number the
-- README quotes.
--
-- `estimated_` is not modesty. The embeddings endpoint returns no usage
-- metadata at all -- no token counts, no billable characters -- so unlike
-- generation, this cost is derived from a local character estimate rather than
-- measured. The column name says so at every call site that reads it.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id                  BIGSERIAL PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at            TIMESTAMPTZ,
    query               TEXT NOT NULL,

    messages_seen       INT NOT NULL DEFAULT 0,
    messages_empty      INT NOT NULL DEFAULT 0,
    chunks_produced     INT NOT NULL DEFAULT 0,
    chunks_duplicate    INT NOT NULL DEFAULT 0,
    chunks_inserted     INT NOT NULL DEFAULT 0,

    embedding_model     TEXT,
    embed_calls         INT NOT NULL DEFAULT 0,
    estimated_tokens    INT NOT NULL DEFAULT 0,
    estimated_cost_usd  NUMERIC(12, 6),

    status              TEXT NOT NULL DEFAULT 'running',
    error               TEXT,

    CONSTRAINT ingest_runs_status_valid CHECK (status IN ('running', 'success', 'failed'))
);

CREATE INDEX IF NOT EXISTS ingest_runs_started_at_idx ON ingest_runs (started_at DESC);
