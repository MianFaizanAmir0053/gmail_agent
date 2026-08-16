-- Idempotency ledger and Gmail sync cursor.
--
-- This is the layer that makes re-running the poller safe. It is a database
-- constraint, not a prompt: no amount of model reasoning stops a scheduler from
-- re-reading the same message after a restart.

CREATE TABLE IF NOT EXISTS processed_messages (
    gmail_message_id  TEXT PRIMARY KEY,
    thread_id         TEXT        NOT NULL,
    status            TEXT        NOT NULL,
    calendar_event_id TEXT,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT processed_messages_status_valid CHECK (
        status IN ('claimed', 'extracted', 'awaiting_approval',
                   'created', 'skipped', 'rejected', 'failed')
    ),
    -- A created event must record which event it created; anything else must not
    -- claim one. Without this the reconciliation story is unauditable.
    CONSTRAINT processed_messages_event_id_matches_status CHECK (
        (status = 'created') = (calendar_event_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS processed_messages_status_idx
    ON processed_messages (status);

CREATE INDEX IF NOT EXISTS processed_messages_thread_idx
    ON processed_messages (thread_id);

-- One row, enforced. The Gmail history cursor is global to the mailbox, so a
-- second row would mean two pollers disagreeing about where they are.
CREATE TABLE IF NOT EXISTS sync_state (
    id              INT PRIMARY KEY DEFAULT 1,
    last_history_id TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT sync_state_single_row CHECK (id = 1)
);

INSERT INTO sync_state (id, last_history_id) VALUES (1, NULL)
ON CONFLICT (id) DO NOTHING;
