-- One row per scheduled tick (M15).
--
-- A poll that finds no mail writes no spans, and `runs` requires a Gmail
-- message id, so neither can show that a quiet tick happened at all. This
-- table can, which is what turns "it ran unattended for eight days" from an
-- assertion into a query.
--
-- `ok` is strict on purpose: a poll that raised, or that marked any message
-- FAILED, is not a success. Failed messages are never offered again, so if
-- such a tick counted as healthy, a dead model path would look fine forever.

CREATE TABLE IF NOT EXISTS job_runs (
    id           BIGSERIAL PRIMARY KEY,
    job          TEXT        NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ NOT NULL,
    ok           BOOLEAN     NOT NULL,
    seen         INT,
    started      INT,
    failed       INT,
    error        TEXT,
    CHECK (finished_at >= started_at)
);

CREATE INDEX IF NOT EXISTS job_runs_job_finished_idx ON job_runs (job, finished_at);
