-- Nothing for Supabase's Data API (M16 review).
--
-- On Supabase, every table created in `public` is granted to `anon` and
-- `authenticated`, the roles its REST and GraphQL endpoints act as. This app
-- uses neither endpoint: Fly connects as the database owner, and the web app
-- as `web_reader` (008). Left in place, those grants would let anyone holding
-- the project's anon key read the ledger, the proposals and the checkpoints,
-- which hold whole emails, and write to them. docs/DEPLOY.md also has the
-- Data API turned off; this removes the grants, so turning it back on
-- exposes nothing.
--
-- The default privileges go too, for tables this role creates later:
-- LangGraph creates its checkpoint tables at run time, after every migration
-- has run. Both forms are revoked, because a schema's defaults cannot take
-- back what the global ones grant.
--
-- A database without these roles (local Postgres, Neon, CI) is left as it
-- was. Every statement can be re-run.

DO $$
DECLARE
    api_role TEXT;
BEGIN
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = api_role) THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', api_role);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', api_role);
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', api_role
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I', api_role
            );
            EXECUTE format('ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM %I', api_role);
            EXECUTE format('ALTER DEFAULT PRIVILEGES REVOKE ALL ON SEQUENCES FROM %I', api_role);
        END IF;
    END LOOP;
END
$$;
