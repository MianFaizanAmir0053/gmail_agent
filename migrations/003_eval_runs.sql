-- Eval history, so the dashboard does not have to read the repository.
--
-- The JSON files under results/ remain the committed evidence -- a reviewer
-- should be able to see the progression in the repo without running anything.
-- This table is a published copy for querying, filled by `app.eval.publish`.
--
-- Keeping publication separate from the harness matters: `app.eval.run` must
-- stay runnable with no database at all, since scoring an extractor has nothing
-- to do with persistence.

CREATE TABLE IF NOT EXISTS eval_runs (
    id            BIGSERIAL PRIMARY KEY,
    source_file   TEXT NOT NULL UNIQUE,
    ran_at        TIMESTAMPTZ NOT NULL,
    extractor     TEXT NOT NULL,
    fixtures      INT NOT NULL,
    exact_match   NUMERIC(5, 4) NOT NULL,
    is_meeting_f1 NUMERIC(5, 4),
    attendees_f1  NUMERIC(5, 4),
    fields        JSONB NOT NULL DEFAULT '{}'::jsonb,
    failures      JSONB NOT NULL DEFAULT '[]'::jsonb,
    published_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS eval_runs_ran_at_idx ON eval_runs (ran_at);
CREATE INDEX IF NOT EXISTS eval_runs_extractor_idx ON eval_runs (extractor);
