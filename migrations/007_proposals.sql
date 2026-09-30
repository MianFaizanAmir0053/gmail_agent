-- The web channel's records (M16, docs/plans/M16-web-channel.md, D2).
--
-- `proposals` mirrors each parked thread for the web app, which reads through
-- a role that cannot see checkpoints. `decisions` is both the queue the worker
-- applies and the permanent record M24 computes autonomy from, so its rows
-- are never deleted; only their content columns are cleared (D8). Its foreign
-- key restricts rather than cascades, so a stray DELETE on the ledger fails
-- instead of quietly erasing that record.
--
-- Every statement can be re-run.

CREATE TABLE IF NOT EXISTS proposals (
    message_id        TEXT PRIMARY KEY
                      REFERENCES processed_messages (gmail_message_id) ON DELETE CASCADE,
    revision          INT         NOT NULL CHECK (revision >= 1),
    status            TEXT        NOT NULL
                      CHECK (status IN ('pending', 'deciding', 'decided', 'failed')),
    final_status      TEXT        CHECK (final_status IN ('created', 'skipped', 'rejected')),
    action_type       TEXT        NOT NULL
                      CHECK (action_type IN ('calendar_hold', 'calendar_invite')),
    pipeline_version  TEXT        NOT NULL,
    payload           JSONB,
    dry_run           BOOLEAN     NOT NULL,
    parked_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status = 'decided') = (final_status IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS proposals_status_idx ON proposals (status, parked_at);

CREATE TABLE IF NOT EXISTS decisions (
    id                BIGSERIAL PRIMARY KEY,
    message_id        TEXT        NOT NULL REFERENCES proposals (message_id) ON DELETE RESTRICT,
    revision          INT         NOT NULL CHECK (revision >= 1),
    action            TEXT        NOT NULL CHECK (action IN ('confirm', 'edit', 'cancel', 'sweep')),
    via               TEXT        NOT NULL CHECK (via IN ('web', 'cli', 'telegram', 'sweep')),
    action_type       TEXT        NOT NULL,
    pipeline_version  TEXT        NOT NULL,
    correction        TEXT,
    decided_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    latency_seconds   DOUBLE PRECISION NOT NULL CHECK (latency_seconds >= 0),
    outcome           TEXT
                      CHECK (outcome IN ('reparked', 'created', 'skipped', 'rejected', 'no_effect', 'failed')),
    reason            TEXT,
    attempts          INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until       TIMESTAMPTZ,
    settled_at        TIMESTAMPTZ,
    CHECK ((outcome IS NULL) = (settled_at IS NULL))
);

-- At most one open decision per proposal. The claim in `decide()` already
-- ensures it; this makes a second one impossible rather than merely unlikely.
CREATE UNIQUE INDEX IF NOT EXISTS decisions_one_open_idx
    ON decisions (message_id) WHERE outcome IS NULL;

-- The worker's scan: open decisions whose next attempt is due.
CREATE INDEX IF NOT EXISTS decisions_due_idx
    ON decisions (next_attempt_at) WHERE outcome IS NULL;

CREATE INDEX IF NOT EXISTS decisions_counting_idx
    ON decisions (action_type, pipeline_version, decided_at);

-- An alert is recorded per channel, and only once that channel delivered it
-- (D6): a row means "delivered there", and its absence means "try again". Per
-- channel, because Telegram accepting an alert says nothing about the phones
-- that only web push reaches.
CREATE TABLE IF NOT EXISTS alerts_sent (
    code     TEXT        NOT NULL,
    subject  TEXT        NOT NULL,
    channel  TEXT        NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (code, subject, channel)
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint      TEXT PRIMARY KEY,
    p256dh        TEXT        NOT NULL,
    auth          TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The iPhone fallback (D4). Used only if Google sign-in fails inside the
-- installed app; the code itself is never stored.
CREATE TABLE IF NOT EXISTS pairing_codes (
    id              BIGSERIAL PRIMARY KEY,
    code_sha256     TEXT        NOT NULL UNIQUE,
    issued_to       TEXT        NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    attempts        INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    redeemed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
