-- The action policy's records (M17, docs/plans/M17-action-policy.md, D8).
--
-- None of these tables holds email content. `outbound_actions`,
-- `model_spend` and `audit_log` are M24's evidence and the budget's, and are
-- kept for good; confirmed contacts stay until the owner removes them.
--
-- Every statement can be re-run.

-- One row: the owner's pause switch, and the spend gate's state (D5, D6).
CREATE TABLE IF NOT EXISTS control (
    id            INT         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    paused        BOOLEAN     NOT NULL DEFAULT false,
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_via   TEXT,
    budget_state  TEXT        NOT NULL DEFAULT 'ok'
                  CHECK (budget_state IN ('ok', 'warning', 'exhausted'))
);

INSERT INTO control (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- What a proposal would run, and the keyed hash of its exact arguments (D2).
ALTER TABLE proposals ADD COLUMN IF NOT EXISTS tool TEXT;
ALTER TABLE proposals ADD COLUMN IF NOT EXISTS args_hash TEXT;

-- The owner asked to withdraw a queued decision; only the worker carries it
-- out, because only the worker may settle a decision it might have applied (D6).
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS withdraw_requested_at TIMESTAMPTZ;

-- One approval per Confirm, bound to the arguments and the mode the owner saw
-- (D2). Restricts deletes, like `decisions`, so the record cannot be erased by
-- a cascade.
CREATE TABLE IF NOT EXISTS outbound_actions (
    id           BIGSERIAL   PRIMARY KEY,
    decision_id  BIGINT      NOT NULL UNIQUE REFERENCES decisions (id) ON DELETE RESTRICT,
    message_id   TEXT        NOT NULL,
    tool         TEXT        NOT NULL,
    tier         SMALLINT    NOT NULL CHECK (tier IN (1, 2)),
    args_hash    TEXT        NOT NULL,
    dry_run      BOOLEAN     NOT NULL,
    nonce        TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'approved'
                 CHECK (status IN ('approved', 'executing', 'done', 'dry_run', 'refused', 'failed')),
    event_id     TEXT,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    -- The id is what lets a later attempt find the event instead of booking a
    -- second one (D3): an action cannot be executing, or done, without it.
    CHECK (status NOT IN ('executing', 'done') OR event_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS outbound_actions_message_idx ON outbound_actions (message_id);

-- Guests the owner allowed once, kept until removed (D4).
CREATE TABLE IF NOT EXISTS confirmed_contacts (
    address     TEXT        PRIMARY KEY,
    allowed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    via         TEXT        NOT NULL CHECK (via IN ('web', 'cli')),
    message_id  TEXT
);

-- Every metered model call, and every call the gate refused (D5). The month's
-- spend is the sum of `cost_usd`; a refusal costs nothing and is still
-- recorded, so "no model was called" can be checked.
CREATE TABLE IF NOT EXISTS model_spend (
    id               BIGSERIAL      PRIMARY KEY,
    at               TIMESTAMPTZ    NOT NULL DEFAULT now(),
    model            TEXT           NOT NULL,
    input_tokens     INT            NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens    INT            NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cached_tokens    INT            NOT NULL DEFAULT 0 CHECK (cached_tokens >= 0),
    thinking_tokens  INT            NOT NULL DEFAULT 0 CHECK (thinking_tokens >= 0),
    cost_usd         NUMERIC(12, 6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    estimated        BOOLEAN        NOT NULL DEFAULT false,
    refused          TEXT           CHECK (refused IN ('unpriced', 'exhausted'))
);

CREATE INDEX IF NOT EXISTS model_spend_at_idx ON model_spend (at);

-- Every attempt to act, and every change to the switches (D7). It holds ids,
-- keyed hashes and fixed phrases, never content, and no foreign keys: a
-- delete elsewhere never cascades into it.
CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL   PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind          TEXT        NOT NULL,
    tool          TEXT,
    tier          SMALLINT,
    args_hash     TEXT,
    dry_run       BOOLEAN,
    outcome       TEXT,
    decision_id   BIGINT,
    message_id    TEXT,
    subject_hash  TEXT,
    reason        TEXT
);

CREATE INDEX IF NOT EXISTS audit_log_at_idx ON audit_log (at DESC);

-- Append-only. This guards against code paths, not against the database's
-- owner: TRUNCATE is not a row operation and is not blocked.
CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger
    LANGUAGE plpgsql AS
$$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END
$$;

CREATE OR REPLACE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();

-- The web app shows the pause and budget state, hides guests already allowed,
-- and lists the audit log on its Activity page (D4-D7).
GRANT SELECT ON control, confirmed_contacts, audit_log TO web_reader;
