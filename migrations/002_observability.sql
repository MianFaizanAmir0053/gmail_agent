-- Traces: one `runs` row per message, one `spans` row per graph node.
--
-- Token counts are stored raw and cost is stored alongside rather than instead.
-- Rates change; if only dollars were kept, history could never be recomputed and
-- every past number would silently mean something different.
--
-- cost_usd is NULLABLE on purpose: an unpriced model records NULL, not 0.
-- Zero is a claim that something was free, which is a lie that quietly
-- under-reports the total.

CREATE TABLE IF NOT EXISTS runs (
    trace_id         UUID PRIMARY KEY,
    gmail_message_id TEXT NOT NULL,
    status           TEXT NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    duration_ms      INT,
    total_cost_usd   NUMERIC(12, 6),
    error            TEXT,

    CONSTRAINT runs_status_valid CHECK (
        status IN ('running', 'awaiting_approval', 'success', 'failed')
    )
);

CREATE INDEX IF NOT EXISTS runs_started_at_idx ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS runs_message_idx ON runs (gmail_message_id);

CREATE TABLE IF NOT EXISTS spans (
    id              BIGSERIAL PRIMARY KEY,
    trace_id        UUID NOT NULL REFERENCES runs (trace_id) ON DELETE CASCADE,
    node            TEXT NOT NULL,
    model           TEXT,
    status          TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    latency_ms      INT NOT NULL,

    input_redacted  JSONB,
    output_redacted JSONB,

    input_tokens    INT NOT NULL DEFAULT 0,
    output_tokens   INT NOT NULL DEFAULT 0,
    cached_tokens   INT NOT NULL DEFAULT 0,
    thinking_tokens INT NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(12, 6),

    retry_count     INT NOT NULL DEFAULT 0,
    error           TEXT,

    CONSTRAINT spans_status_valid CHECK (status IN ('ok', 'error'))
);

CREATE INDEX IF NOT EXISTS spans_trace_idx ON spans (trace_id);
CREATE INDEX IF NOT EXISTS spans_started_at_idx ON spans (started_at DESC);
CREATE INDEX IF NOT EXISTS spans_node_idx ON spans (node);
