-- An outage is not the message's failure (M20's open question, closed on
-- 2026-10-18 in docs/plans/M20-mail-sync.md). A message the model or Gmail
-- could not read for an outage goes back to the feed, which waits before it
-- offers the message again. Each outage is counted on the message's row, and
-- the last one allowed dead-letters it, as any other failure. Additive, and
-- every statement can be re-run.
ALTER TABLE gmail_messages
    ADD COLUMN IF NOT EXISTS outages INT NOT NULL DEFAULT 0 CHECK (outages >= 0);
ALTER TABLE gmail_messages ADD COLUMN IF NOT EXISTS last_outage_at TIMESTAMPTZ;
