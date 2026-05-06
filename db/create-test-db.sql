-- ============================================================
-- v1.7.0: dedicated test database
-- ============================================================
-- The test conftest TRUNCATEs 39 tables at session start. Pre-v1.7.0
-- it ran that wipe against the application database (DATABASE_URL),
-- which was a real footgun once v1.7.0 introduced operator-managed
-- state (inbound_source_systems_allowlist + cross_system_mappings).
--
-- This script runs ONCE during the postgres image's first-boot init
-- (docker-entrypoint-initdb.d) and creates an empty `sentry_test`
-- database. The conftest connects via TEST_DATABASE_URL to this DB
-- and the application code keeps using `sentry` via DATABASE_URL.
--
-- Existing volumes won't pick this up without `docker compose down -v`
-- (the postgres image only runs init scripts on a virgin data dir).
-- See docs/deployment.md for the bootstrap sequence and the
-- sentry-v1_7_0-premerge-gate.md pre-flight.
-- ============================================================

CREATE DATABASE sentry_test;
GRANT ALL PRIVILEGES ON DATABASE sentry_test TO sentry;
