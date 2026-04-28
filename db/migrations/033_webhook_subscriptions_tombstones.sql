-- ============================================================
-- Migration 033: webhook_subscriptions_tombstones (v1.6.0 #168)
-- ============================================================
-- Foundation table for the URL-reuse acknowledgement gate on
-- the v1.6.0 outbound webhook admin surface. Mirrors the V-207
-- #148 pattern (consumer_groups_tombstones, migration 027) but
-- with per-deletion history rather than a single most-recent
-- row, because the URL-reuse gate needs to surface every prior
-- subscription that pointed at the same delivery_url, not just
-- the latest one.
--
-- Threat model the gate closes:
--
--   An admin hard-deletes a subscription pointing at
--   https://consumer.example/hook and then a different admin
--   creates a new subscription with the same delivery_url. The
--   new subscription begins with last_delivered_event_id = 0
--   and replays every visible event the prior subscription's
--   downstream consumer already ingested. If the consumer is
--   not strictly idempotent on event_id (the v1.6 wire contract
--   documents the dedupe key but does not enforce it), inventory
--   state at the consumer drifts silently.
--
--   The same shape weaponizes a URL takeover: an attacker who
--   acquires the DNS or TLS endpoint between deletion and
--   recreation can wait for the new subscription to start
--   delivering and harvest the entire event stream.
--
-- The gate runs at admin POST time: query
--   SELECT * FROM webhook_subscriptions_tombstones
--    WHERE delivery_url_at_delete = :new_url
--      AND acknowledged_at IS NULL
-- and, if any rows return, refuse the create with
--   409 url_reuse_unacknowledged
-- unless the request body carries acknowledge_url_reuse: true.
-- The acknowledgement stamps acknowledged_at + acknowledged_by
-- on every matching tombstone so subsequent unrelated CREATEs
-- against the same URL do not retrip the guard.
--
-- Tombstones are NEVER deleted; the table is forensic history.
-- The partial index covers only unacknowledged tombstones so
-- the URL-reuse query stays fast as the table accumulates
-- acknowledged rows.
--
-- subscription_id has NO FK to webhook_subscriptions because
-- the source row no longer exists by the time the tombstone is
-- written. The reference is logical only; investigators chase
-- it via webhook_subscriptions_audit (migration 032) or via
-- audit_log entries from the admin DELETE handler (lands in F5
-- consumer code, not here).
--
-- deleted_by is NOT NULL because the admin endpoint always
-- runs under cookie auth (no anonymous DELETE path); the FK
-- to users captures forensic identity. acknowledged_by is
-- nullable until the gate is cleared, and FK-checks against
-- users when set so the trail is bindable.
--
-- Wrapped in BEGIN/COMMIT per V-213 #152 discipline so a
-- partial apply cannot leave the table without its index;
-- a missing partial index would silently turn the URL-reuse
-- gate into a full-table TEXT scan on every webhook create.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS webhook_subscriptions_tombstones (
    tombstone_id            BIGSERIAL    PRIMARY KEY,
    subscription_id         UUID         NOT NULL,
    delivery_url_at_delete  TEXT         NOT NULL,
    connector_id            VARCHAR(64)  NOT NULL,
    deleted_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    deleted_by              INTEGER      NOT NULL REFERENCES users(user_id),
    acknowledged_at         TIMESTAMPTZ,
    acknowledged_by         INTEGER      REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS webhook_subscriptions_tombstones_url_unack
    ON webhook_subscriptions_tombstones (delivery_url_at_delete)
    WHERE acknowledged_at IS NULL;

COMMIT;
