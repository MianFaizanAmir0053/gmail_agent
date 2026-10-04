-- The chat turn (M19, docs/plans/M19-planner.md, D1, D8 and D12). Additive,
-- and every statement can be re-run.

-- One row per request the owner typed in the web app. The typed request and
-- the pasted text are kept apart, as the browser marked them (D5): the
-- planner reads only the first. The plan is the checked one; the answer is
-- what code rendered from it (D7), with the paste's addresses for an Allow
-- and the cards it made. A turn runs once: a write applies only while it is
-- `running`, and one stranded by a restart is failed, never retried (D1).
CREATE TABLE IF NOT EXISTS chat_turns (
    id               BIGSERIAL PRIMARY KEY,
    idempotency_key  TEXT        NOT NULL UNIQUE
                     CHECK (idempotency_key ~ '^[A-Za-z0-9_-]{16,64}$'),
    typed            TEXT,
    pasted           TEXT,
    asked_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    status           TEXT        NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'answered', 'stopped', 'failed')),
    started_at       TIMESTAMPTZ,
    plan             JSONB,
    answer           JSONB,
    reason           TEXT,
    finished_at      TIMESTAMPTZ,
    CHECK ((status IN ('answered', 'stopped', 'failed')) = (finished_at IS NOT NULL)),
    CHECK ((status = 'queued') = (started_at IS NULL))
);

-- The job's scan for queued turns, and the sweep for stranded running ones.
CREATE INDEX IF NOT EXISTS chat_turns_status_idx ON chat_turns (status, asked_at);

-- One queue, two sources (D8). A chat proposal's ledger row and proposal say
-- so; every row before M19 is mail's.
ALTER TABLE processed_messages
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'mail'
    CHECK (source IN ('mail', 'chat'));

ALTER TABLE proposals
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'mail'
    CHECK (source IN ('mail', 'chat'));
ALTER TABLE proposals
    ADD COLUMN IF NOT EXISTS chat_turn_id BIGINT REFERENCES chat_turns (id) ON DELETE RESTRICT;
ALTER TABLE proposals DROP CONSTRAINT IF EXISTS proposals_chat_turn_check;
ALTER TABLE proposals ADD CONSTRAINT proposals_chat_turn_check
    CHECK ((source = 'chat') = (chat_turn_id IS NOT NULL));

CREATE INDEX IF NOT EXISTS proposals_chat_turn_idx
    ON proposals (chat_turn_id) WHERE chat_turn_id IS NOT NULL;

-- The web app may read a turn's status, its typed request, its rendered
-- answer and its reason (decision 12): never the pasted text, nor the plan.
-- A column grant, so a column added later is not readable until granted;
-- revoked first, so running this again leaves exactly this grant.
REVOKE ALL ON chat_turns FROM web_reader;
GRANT SELECT (id, status, asked_at, typed, answer, reason) ON chat_turns TO web_reader;
