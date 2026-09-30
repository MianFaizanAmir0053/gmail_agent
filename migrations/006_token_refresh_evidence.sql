-- Which Google token a `token_refresh` row is about (M15).
--
-- "In production removes the seven-day limit" is confirmed by one thing
-- only: a successful refresh of a production-minted token after its seventh
-- day. That evidence has to be tied to the token it came from. Otherwise the
-- death of an old token (or the success of a new one) would be read as the
-- fate of whichever token is current.

ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS token_issued_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS job_runs_token_idx
    ON job_runs (token_issued_at, finished_at)
    WHERE job = 'token_refresh';
