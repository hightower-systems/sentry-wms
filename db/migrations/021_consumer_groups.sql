-- ============================================================
-- Migration 021: connectors + consumer_groups tables (v1.5.0)
-- ============================================================
-- Two new tables that support consumer-group polling against the
-- v1.5.0 integration_events outbox:
--
-- 1. ``connectors`` is minimal in v1.5.0 (just PK + display_name +
--    timestamps). v1.9 expands this table to the full shape from the
--    framework doc (section 12.1). Landing the PK now means the
--    tables that reference connector_id (``consumer_groups`` here,
--    ``wms_tokens`` in migration 023, ``webhook_deliveries`` in v1.6)
--    can adopt the FK without a later rename migration.
--
-- 2. ``consumer_groups`` tracks per-group cursor state so a connector
--    can resume after a crash or disconnect. Each group has its own
--    ``last_cursor`` (advanced by POST /api/v1/events/ack) plus an
--    optional ``subscription`` JSONB filter. ``last_heartbeat`` is
--    updated by the polling handler, throttled to every 30s per plan
--    Decision T.
-- ============================================================

CREATE TABLE IF NOT EXISTS connectors (
    connector_id VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(128) NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS consumer_groups (
    consumer_group_id VARCHAR(64)  PRIMARY KEY,
    connector_id      VARCHAR(64)  NOT NULL REFERENCES connectors(connector_id),
    last_cursor       BIGINT       NOT NULL DEFAULT 0,
    last_heartbeat    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    subscription      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_consumer_groups_connector
    ON consumer_groups (connector_id);
