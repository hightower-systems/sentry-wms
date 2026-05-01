-- ============================================================
-- Least-privilege DB role for webhook-dispatcher (v1.6.0 #169)
-- ============================================================
-- v1.6.0 introduces an outbound webhook dispatcher that POSTs
-- visible integration_events to consumer-supplied URLs. Pre-v1.6.0
-- the only daemon role partition was V-214 #153 (snapshot-keeper);
-- the dispatcher follows the identical shape so a compromise of
-- the api host does not grant the attacker every permission the
-- dispatcher has, and vice versa.
--
-- Grants cover exactly the dispatcher's need-to-know:
--
--   * CONNECT on the database (and USAGE on the public schema)
--   * SELECT on integration_events (cursor read: "what's next?")
--   * SELECT / UPDATE on webhook_subscriptions (cursor advance,
--     auto-pause status flip with pause_reason)
--   * INSERT / SELECT / UPDATE on webhook_deliveries (per-attempt
--     log INSERT + state transitions
--     pending -> in_flight -> succeeded | failed | dlq;
--     append-only with the one DLQ-flip exception)
--   * USAGE on the webhook_deliveries_delivery_id_seq sequence
--     (BIGSERIAL inserts need it; without USAGE the INSERT
--     surfaces a 'permission denied for sequence' error)
--   * SELECT on webhook_secrets (decrypt for HMAC signing)
--   * LISTEN on integration_events_visible (migration 031) and
--     webhook_subscription_events (D3) -- not granted explicitly;
--     any role with CONNECT can LISTEN. Channels listed here for
--     operator awareness.
--
-- Explicitly NOT granted:
--
--   * users, wms_tokens, wms_tokens_audit, audit_log, connectors
--     (writes) -- the dispatcher has no admin / token / forensic
--     write surface; a compromise of the dispatcher must not
--     reach the auth or audit layers.
--   * webhook_subscriptions_audit, webhook_secrets_audit -- the
--     V-157-style triggers fire under the role doing the DELETE;
--     the dispatcher does not DELETE either source table, so it
--     cannot write to the audit shadows.
--   * webhook_subscriptions_tombstones -- tombstones are an
--     admin-endpoint concern (A1, lands later); the dispatcher
--     has no need to read or write them.
--
-- This script is operator-driven, not auto-applied. Migrations
-- cannot read a password from the environment and we do not want
-- to bake a placeholder password into git. Run it once per
-- deployment after the v1.6.0 upgrade; the operator supplies the
-- password via a psql variable.
--
--   psql -v sentry_dispatcher_password=<strong-password> \
--        -U <db-superuser> -d <database> \
--        -f db/role-dispatcher.sql
--
-- (Pass the password unquoted; the script wraps it as an SQL
-- string literal via psql's :'var' interpolation. The
-- interpolation is performed at the top level, NOT inside a
-- DO $$ ... $$ block: psql does not substitute :'var' inside
-- dollar-quoted strings, so the V-214 #153 sibling pattern is
-- replaced here with \gexec at the top level. The two CREATE /
-- ALTER ROLE branches are guarded by WHERE clauses so exactly
-- one fires per run, and a second run only runs the ALTER
-- branch.)
--
-- Then set DISPATCHER_DATABASE_URL in .env to
--   postgresql://sentry_dispatcher:<strong-password>@db:5432/<database>
-- and (after the D1 commit) restart the webhook-dispatcher container.
--
-- Idempotent: safe to re-run. Both \gexec branches are predicated
-- on pg_roles existence so re-runs only ALTER (which carries the
-- new password); the GRANTs are themselves idempotent.
--
-- ON_ERROR_STOP discipline: the V-214 #170 regression on the
-- snapshot-keeper sibling shipped because psql exited 0 despite
-- a cascade of role-creation failures. Setting ON_ERROR_STOP at
-- the top of every operator-run script means a future failure
-- exits non-zero and deployment automation flags the step.
-- ============================================================

\set ON_ERROR_STOP on

-- Branch 1: CREATE ROLE on first run.
SELECT format('CREATE ROLE sentry_dispatcher LOGIN PASSWORD %L',
              :'sentry_dispatcher_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentry_dispatcher')
\gexec

-- Branch 2: ALTER ROLE on subsequent runs (also rotates the
-- password if the operator supplies a new value).
SELECT format('ALTER ROLE sentry_dispatcher WITH LOGIN PASSWORD %L',
              :'sentry_dispatcher_password')
 WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentry_dispatcher')
\gexec

-- GRANT CONNECT needs a literal database name; derive it from
-- current_database() so the script works regardless of whether
-- the operator deployed with POSTGRES_DB=sentry or a custom value.
SELECT format('GRANT CONNECT ON DATABASE %I TO sentry_dispatcher',
              current_database())
\gexec

GRANT USAGE ON SCHEMA public TO sentry_dispatcher;

GRANT SELECT                       ON integration_events    TO sentry_dispatcher;
GRANT SELECT, UPDATE               ON webhook_subscriptions TO sentry_dispatcher;
GRANT INSERT, SELECT, UPDATE       ON webhook_deliveries    TO sentry_dispatcher;
GRANT USAGE                        ON SEQUENCE webhook_deliveries_delivery_id_seq TO sentry_dispatcher;
GRANT SELECT                       ON webhook_secrets       TO sentry_dispatcher;
