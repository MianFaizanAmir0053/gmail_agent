-- The mail sync's records (M20, docs/plans/M20-mail-sync.md, D1, D2 and D8).
--
-- Metadata only. No column here could hold a subject, a snippet or a body:
-- they can carry one-time codes, and wait for M18 to strip them. Nothing is
-- granted to `web_reader` -- the web app shows none of this in M20 -- and
-- 009's revoked defaults keep it from Supabase's Data API roles.
--
-- `sync_state`, the single row the old poller writes, stays as it is:
-- migrations are additive, and the first sync run reads it (D3).
--
-- Every statement can be re-run.

-- One row per message, per mailbox: one table per source (M23 adds its own).
CREATE TABLE IF NOT EXISTS gmail_messages (
    account               TEXT        NOT NULL,
    message_id            TEXT        NOT NULL,
    thread_id             TEXT        NOT NULL,
    -- Gmail's `internalDate`: when it received or sent the message. Never
    -- the Date header, which the sender writes.
    internal_at           TIMESTAMPTZ NOT NULL,
    label_ids             TEXT[]      NOT NULL DEFAULT '{}',
    direction             TEXT        NOT NULL CHECK (direction IN ('in', 'out')),
    to_self               BOOLEAN     NOT NULL,
    -- Promotions and Social are never stored, but a stored row can move
    -- there afterwards, and its category follows its labels.
    category              TEXT        NOT NULL
                          CHECK (category IN ('primary', 'updates', 'forums',
                                              'promotions', 'social')),
    from_addr             TEXT,
    to_addrs              TEXT[]      NOT NULL DEFAULT '{}',
    cc_addrs              TEXT[]      NOT NULL DEFAULT '{}',
    -- The bulk signals, raw: a flag rather than the header's URL.
    has_list_unsubscribe  BOOLEAN     NOT NULL,
    precedence            TEXT,
    auto_submitted        TEXT,
    -- How the row first arrived. Diagnosis only: the feed decides by time.
    arrived_via           TEXT        NOT NULL
                          CHECK (arrived_via IN ('history', 'switch_over', 'catch_up',
                                                 'recall', 'queue', 'backfill')),
    first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set by `messageDeleted` or a fetch's 404, never for not being listed:
    -- archived or recategorised mail is not gone.
    gone_at               TIMESTAMPTZ,
    PRIMARY KEY (account, message_id)
);

CREATE INDEX IF NOT EXISTS gmail_messages_thread_idx
    ON gmail_messages (account, thread_id);

CREATE INDEX IF NOT EXISTS gmail_messages_internal_at_idx
    ON gmail_messages (account, internal_at);

-- The meeting pipeline's feed (D4): strictly inbound Primary mail still in
-- the mailbox. The rest of the rule is checked on the few rows this leaves.
CREATE INDEX IF NOT EXISTS gmail_messages_feed_idx
    ON gmail_messages (account, internal_at)
    WHERE direction = 'in' AND category = 'primary' AND NOT to_self AND gone_at IS NULL;

-- One row per mailbox. Every write to it is conditional on the value it
-- replaces (D2), so two writers can never both move it.
CREATE TABLE IF NOT EXISTS gmail_cursors (
    account         TEXT        PRIMARY KEY,
    history_id      TEXT        NOT NULL,
    -- The switch-over instant: the feed takes nothing older than an hour
    -- before it, and the backfill's windows end at it.
    feed_from       TIMESTAMPTZ NOT NULL,
    switch_over_at  TIMESTAMPTZ,
    -- The last pass that reached the end of history. Liveness is judged by
    -- it, and a catch-up's gap starts an hour before it.
    caught_up_at    TIMESTAMPTZ,
    backfill_until  TIMESTAMPTZ NOT NULL,
    gap_from        TIMESTAMPTZ,
    gap_until       TIMESTAMPTZ,
    gap_progress    TIMESTAMPTZ,
    catch_ups       INT         NOT NULL DEFAULT 0 CHECK (catch_ups >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((gap_from IS NULL) = (gap_until IS NULL)),
    CHECK (gap_progress IS NULL OR gap_from IS NOT NULL)
);

-- Messages to fetch outside the incremental pass (D3): one that failed on
-- its own, one a label change could bring in, and a catch-up's work. Five
-- strikes make a message `unreadable`; an outage strikes nobody.
CREATE TABLE IF NOT EXISTS gmail_fetch_queue (
    message_id  TEXT        PRIMARY KEY,
    reason      TEXT        NOT NULL
                CHECK (reason IN ('fetch_failed', 'label_change', 'catch_up', 'refetch')),
    queued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    strikes     INT         NOT NULL DEFAULT 0 CHECK (strikes >= 0),
    status      TEXT        NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'unreadable'))
);

CREATE INDEX IF NOT EXISTS gmail_fetch_queue_status_idx
    ON gmail_fetch_queue (status, queued_at);
