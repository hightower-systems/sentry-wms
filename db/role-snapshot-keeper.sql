-- ============================================================
-- Least-privilege DB role for snapshot-keeper (v1.5.1 V-214 #153,
-- regression fix #170)
-- ============================================================
-- Pre-v1.5.1 the snapshot-keeper service shared DATABASE_URL with
-- the api container and ran under the full `sentry` role. A
-- separate compromise of the api host then gave the attacker every
-- permission the keeper had (and vice versa). V-214 narrows the
-- blast radius by defining a dedicated role whose grants cover
-- exactly the keeper's need-to-know:
--
--   * CONNECT on the database (and USAGE on the public schema)
--   * SELECT on integration_events (snapshot_event_id scan)
--   * SELECT / UPDATE / DELETE on snapshot_scans (promotion +
--     reap lifecycle)
--   * EXECUTE on pg_export_snapshot() (granted PUBLIC by default
--     in PostgreSQL; listed here for completeness)
--   * LISTEN on channels the keeper subscribes to (no explicit
--     grant required; any CONNECT'd role can LISTEN)
--
-- Regression history (#170, fixed 2026-04-28):
--
--   The original v1.5.1 script wrapped CREATE ROLE / ALTER ROLE
--   in DO $$ ... $$; blocks that referenced the password via
--   :'sentry_keeper_password'. psql does NOT perform :'var'
--   substitution inside dollar-quoted strings; the literal text
--   passes through to the server's SQL parser and surfaces
--   ERROR: syntax error at or near ":". The script also lacked
--   \set ON_ERROR_STOP on, so psql exited 0 despite the error
--   cascade. Operator deployments since v1.5.1 silently failed
--   to create the role; snapshot-keeper containers fell back to
--   the api role with full grants instead of the documented
--   subset. This file now uses \gexec at the top level (where
--   psql variable interpolation works) and sets ON_ERROR_STOP
--   so any future failure exits non-zero. Same pattern as
--   db/role-dispatcher.sql (v1.6.0 #169).
--
-- This script is operator-driven, not auto-applied. Migrations
-- cannot read a password from the environment and we do not want
-- to bake a placeholder password into git. Run it once per
-- deployment after the v1.5.1 upgrade; the operator supplies the
-- password via a psql variable.
--
--   psql -v sentry_keeper_password=<strong-password> \
--        -U <db-superuser> -d <database> \
--        -f db/role-snapshot-keeper.sql
--
-- (Pass the password unquoted; the script wraps it as an SQL
-- string literal via psql's :'var' interpolation. The variable
-- name `sentry_keeper_password` is preserved from the v1.5.1
-- shape so existing operator runbooks keep working.)
--
-- Then set SNAPSHOT_KEEPER_DATABASE_URL in .env to
--   postgresql://sentry_keeper:<strong-password>@db:5432/<database>
-- and restart the snapshot-keeper container.
--
-- Idempotent: safe to re-run. The CREATE branch's WHERE clause
-- filters it out on subsequent runs; the ALTER branch fires
-- alone and rotates the password if the operator supplies a
-- new value. The GRANTs are themselves idempotent.
-- ============================================================

\set ON_ERROR_STOP on

-- Branch 1: CREATE ROLE on first run.
SELECT format('CREATE ROLE sentry_keeper LOGIN PASSWORD %L',
              :'sentry_keeper_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentry_keeper')
\gexec

-- Branch 2: ALTER ROLE on subsequent runs (also rotates the
-- password if the operator supplies a new value).
SELECT format('ALTER ROLE sentry_keeper WITH LOGIN PASSWORD %L',
              :'sentry_keeper_password')
 WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentry_keeper')
\gexec

-- GRANT CONNECT needs a literal database name; derive it from
-- current_database() so the script works regardless of whether the
-- operator deployed with POSTGRES_DB=sentry or a custom value.
SELECT format('GRANT CONNECT ON DATABASE %I TO sentry_keeper',
              current_database())
\gexec

GRANT USAGE ON SCHEMA public TO sentry_keeper;

GRANT SELECT ON integration_events TO sentry_keeper;
GRANT SELECT, UPDATE, DELETE ON snapshot_scans TO sentry_keeper;

-- pg_export_snapshot() is PUBLIC by default; keep the explicit
-- grant here so a future lockdown that revokes PUBLIC does not
-- silently break the keeper.
GRANT EXECUTE ON FUNCTION pg_export_snapshot() TO sentry_keeper;
