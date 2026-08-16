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

**The SDK silently collapses a batch, and which batch depends on the model.**
Passing `list[str]` to `embed_content` returns one vector per string on
`gemini-embedding-001` and a *single* vector for the whole list on
`gemini-embedding-2` — bare strings are packed into one multi-part `Content`,
which the newer model reads as one document. Three chunks in, one vector out.
Nothing raises; code that zips inputs to outputs just files every embedding
against the wrong chunk, and the only symptom is retrieval that is confidently
irrelevant, forever. Passing an explicit `list[Content]` behaves identically on
both models, so that is what `app/rag/embed.py` does, and it refuses to return a
batch whose length does not match its input under any circumstances.

**Dimensions are a database constraint, not a tuning knob.** pgvector's `vector`
type can only be HNSW-indexed up to 2000 dimensions, and the model returns 3072
by default. 1536 is a real Matryoshka prefix rather than a truncation, and it
quarters the index memory. Below 3072 the vectors also come back **un-normalised**
— a norm of about 0.69 — which cosine distance forgives and an inner-product
operator would not, so they are normalised on the way in.

**The embeddings endpoint reports no usage at all.** No token counts, no
billable characters. Unlike generation, this cost cannot be measured, only
estimated from characters, so every column and field carrying it is named
`estimated_`. Folding it into `app/obs/pricing.py` would have let an estimate be
summed next to measurements as though it meant the same thing.

**Dedupe belongs before the embedding call, not on insert.** `ON CONFLICT DO
NOTHING` alone satisfies the exit criterion — zero new rows — while costing full
price every run, because each chunk is embedded on the way to being discarded.
Hashing first and asking the database which hashes it already holds turns a
re-run into one `SELECT`. Both properties are asserted separately in the tests;
only the second one is actually load-bearing.

**Ingestion stats are their own table.** `runs` requires a Gmail message ID and
describes a graph execution, so a bulk backfill would have to invent one — and
the dashboard divides spend by messages processed, so folding a thousand-thread
backfill into it would wreck "cost per 100 emails", a number the README quotes.

### What reading the real output caught

Four defects survived a green test suite and were only visible in `--plan`:

- `&#128206;` in message bodies. `extract_body` had a hand-written table of five
  named entities and left every numeric one alone. Replaced with `html.unescape`.
- Flattened HTML tables indent every value by twenty spaces. Pure padding inside
  a budget measured in characters — one chunk went from 795 to 599 chars.
- A sign-off, a name, a phone number and a URL wrapped onto one long line reads
  as prose by every length test. Now cut on formal closings only, and only when
  little follows. `Thanks` and `Cheers` are deliberately excluded: "Thanks, see
  you Thursday at 3" is a sign-off *and* the entire content of the message.
- `Please do not reply to this email` is boilerplate and was not on the list.

Removing a quoted line also has to consume its newline, or every deleted line
becomes a blank one — and a blank line is a paragraph boundary, so chunks were
splitting along seams that existed only because something was deleted there.

**`MIN_CHARS` was wrong at 25.** It dropped "Sounds good" — an agreement, from a
system whose job is remembering what was agreed. Lowered to 8, which still
removes bare "ok". Length is a poor proxy for meaning; M12 is where this stops
being a guess.

**The HNSW index is not being used, and that is correct.** At 29 rows the planner
prefers a sequential scan. Forcing `enable_seqscan = off` confirms the index
*can* serve `<=>` — which is the part worth checking, because an ops-class
mismatch would go unnoticed until the corpus was large enough to matter.

**Privacy, recorded rather than solved.** The corpus contains third parties'
names, postal addresses, and phone numbers, because that is exactly the context
retrieval exists to provide — `app/obs/redact.py` would destroy the entity
information M11 needs. So `chunks` carries the same sensitivity as the mailbox
itself and must never leave a local or managed database.

### Verified

```
ruff / mypy --strict   clean
pytest                 375 passed

run 1   25 messages -> 29 chunks, 29 inserted, 1 embed request, ~$0.000875 (est.)
run 2   25 messages -> 29 chunks,  0 inserted, 0 embed requests, $0
```

Retrieval sanity check, three queries never phrased like the source text:

```
"which job applications were rejected?"      0.660  Update on Your Application...
"pharmacy prescription order for a customer" 0.714  Prescription Sent to Pharmacy...
"billing limit reached on a cloud service"   0.703  Plan usage reached 100% of limit...
```
