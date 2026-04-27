-- ============================================================
-- Migration 029: webhook_subscriptions + webhook_secrets (v1.6.0 #165)
-- ============================================================
-- Foundation table set for the v1.6.0 outbound webhook dispatcher.
-- A webhook subscription is a long-lived opt-in to have visible
-- integration_events POSTed to a consumer-supplied URL with
-- retries, HMAC signing, and a dead-letter lane for failures.
-- The subscription table holds per-consumer state; the secrets
-- table holds the two-slot dual-accept signing material.
--
-- Key shape decisions (locked at the top of the v1.6.0 plan,
-- not re-decided per-emit-site as v1.5.0 did):
--
--   * subscription_id is UUID, not BIGSERIAL. Admin URLs need
--     an opaque identifier to keep enumeration off the table.
--
--   * connector_id FKs the v1.5.0 connectors table so a
--     subscription is always tied to a registered connector.
--     Cascade rules are intentionally absent on this FK; a
--     connector with active subscriptions cannot be removed.
--
--   * subscription_filter is JSONB and the column name is
--     deliberately distinct from consumer_groups.subscription
--     (migration 021) so a code or runbook reference is
--     unambiguous.
--
--   * status is one of 'active' | 'paused' | 'revoked'. Soft
--     delete writes 'revoked'; the dispatcher auto-pauses to
--     'paused' on DLQ-ceiling or pending-ceiling crossings and
--     populates pause_reason. Status validation is application
--     side; this migration enforces only that the column exists
--     with a default of 'active'.
--
--   * rate_limit_per_second + pending_ceiling + dlq_ceiling all
--     carry CHECK ranges that bound the smaller-than-deployment-
--     hard-cap floor. The admin endpoint enforces the upper
--     bound against deployment-wide env-var hard caps
--     (DISPATCHER_MAX_PENDING_HARD_CAP / DISPATCHER_MAX_DLQ_HARD_CAP);
--     the column-level CHECK is the bottom rung that catches
--     bypass paths around the admin layer.
--
--   * delivery_url CHECK is permissive (^https?://) so dev/CI
--     can use http; production HTTPS-only enforcement lives in
--     the admin endpoint via SENTRY_ALLOW_HTTP_WEBHOOKS opt-out
--     (separate commit). Putting the strict gate in the column
--     would block the dev path; putting only an application gate
--     would leave a bypass.
--
--   * Partial index on status='active' is the dispatcher
--     subscription-list query path; full-table scans on a
--     three-value column would not benefit from a btree on the
--     status column itself.
--
-- webhook_secrets shape:
--
--   * Composite PK (subscription_id, generation) with
--     CHECK (generation IN (1, 2)) -- two-slot dual-accept
--     rotation. generation=1 is primary (signed with), generation=2
--     is previous (consumer accepts until expires_at).
--
--   * secret_ciphertext is BYTEA. Fernet encrypts with the
--     existing v1.3 SENTRY_ENCRYPTION_KEY vault; the dispatcher
--     decrypts at signing time because computing HMAC requires
--     plaintext. The plaintext leaves the database exactly twice:
--     once on create / rotate (returned to admin in the response
--     body, ONCE) and once per signing (decrypted into a local
--     variable inside the signer, never logged).
--
--   * ON DELETE CASCADE on subscription_id keeps secret rows
--     from outliving their subscription.
--
--   * expires_at is NULL on the primary; rotation sets it 24h
--     out on the demoted row. The cleanup beat drops expired
--     gen=2 rows.
--
-- Wrapped in BEGIN/COMMIT per V-213 #152 discipline so a
-- partial apply cannot leave webhook_subscriptions present
-- without webhook_secrets (the FK target relationship would be
-- recoverable but the secret-vs-subscription invariant the
-- application relies on would be silently violated until the
-- next deploy ran the migration runner again).
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    subscription_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    connector_id             VARCHAR(64)  NOT NULL REFERENCES connectors(connector_id),
    display_name             VARCHAR(128) NOT NULL,
    delivery_url             TEXT         NOT NULL,
    subscription_filter      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    last_delivered_event_id  BIGINT       NOT NULL DEFAULT 0,
    status                   VARCHAR(16)  NOT NULL DEFAULT 'active',
    pause_reason             VARCHAR(32),
    rate_limit_per_second    INTEGER      NOT NULL DEFAULT 50,
    pending_ceiling          INTEGER      NOT NULL DEFAULT 10000,
    dlq_ceiling              INTEGER      NOT NULL DEFAULT 1000,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT webhook_subscriptions_delivery_url_scheme
        CHECK (delivery_url ~ '^https?://'),
    CONSTRAINT webhook_subscriptions_rate_limit_range
        CHECK (rate_limit_per_second BETWEEN 1 AND 100),
    CONSTRAINT webhook_subscriptions_pending_ceiling_range
        CHECK (pending_ceiling BETWEEN 100 AND 100000),
    CONSTRAINT webhook_subscriptions_dlq_ceiling_range
        CHECK (dlq_ceiling BETWEEN 10 AND 10000)
);

CREATE INDEX IF NOT EXISTS webhook_subscriptions_status
    ON webhook_subscriptions (status)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS webhook_secrets (
    subscription_id     UUID         NOT NULL REFERENCES webhook_subscriptions(subscription_id) ON DELETE CASCADE,
    generation          SMALLINT     NOT NULL,
    secret_ciphertext   BYTEA        NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ,
    PRIMARY KEY (subscription_id, generation),
    CONSTRAINT webhook_secrets_generation_range
        CHECK (generation IN (1, 2))
);

COMMIT;
