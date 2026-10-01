-- The web app's database role (M16, D5).
--
-- The web app reads through this role and writes nothing: every write goes
-- through the Fly API, where `decide()` holds the rules. It reads exactly
-- what the app shows -- the analytics pages (runs, spans, eval_runs) and the
-- timeline (proposals, decisions) -- and nothing on the mailbox side, the
-- graph's checkpoints, or push.
--
-- Created NOLOGIN. The owner gives it a password once, by hand, in Supabase's
-- SQL editor (`ALTER ROLE web_reader LOGIN PASSWORD '...'`), so the password
-- is never in the repo. Every statement can be re-run.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'web_reader') THEN
        CREATE ROLE web_reader NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO web_reader;
GRANT SELECT ON runs, spans, eval_runs, proposals, decisions TO web_reader;
