-- ============================================================
-- Least-privilege DB role for snapshot-keeper (v1.5.1 V-214 #153)
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
-- This script is operator-driven, not auto-applied. Migrations
-- cannot read a password from the environment and we do not want
-- to bake a placeholder password into git. Run it once per
-- deployment after the v1.5.1 upgrade; the operator supplies the
-- password via a psql variable.
--
--   psql -v sentry_keeper_password="'<strong-password>'" \
--        -U <db-superuser> -d <database> \
--        -f db/role-snapshot-keeper.sql
--
-- Then set SNAPSHOT_KEEPER_DATABASE_URL in .env to
--   postgresql://sentry_keeper:<strong-password>@db:5432/<database>
-- and restart the snapshot-keeper container.
--
-- Idempotent: safe to re-run. CREATE ROLE is guarded by a DO block;
-- the GRANTs are themselves idempotent.
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentry_keeper') THEN
        EXECUTE format(
            'CREATE ROLE sentry_keeper LOGIN PASSWORD %L',
            :'sentry_keeper_password'
        );
    ELSE
        EXECUTE format(
            'ALTER ROLE sentry_keeper WITH LOGIN PASSWORD %L',
            :'sentry_keeper_password'
        );
    END IF;
END
$$;

-- GRANT CONNECT needs a literal database name; derive it from
-- current_database() so the script works regardless of whether the
-- operator deployed with POSTGRES_DB=sentry or a custom value.
DO $$
DECLARE
    db_name TEXT := current_database();
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO sentry_keeper', db_name);
END
$$;

GRANT USAGE ON SCHEMA public TO sentry_keeper;

GRANT SELECT ON integration_events TO sentry_keeper;
GRANT SELECT, UPDATE, DELETE ON snapshot_scans TO sentry_keeper;

-- pg_export_snapshot() is PUBLIC by default; keep the explicit
-- grant here so a future lockdown that revokes PUBLIC does not
-- silently break the keeper.
GRANT EXECUTE ON FUNCTION pg_export_snapshot() TO sentry_keeper;
