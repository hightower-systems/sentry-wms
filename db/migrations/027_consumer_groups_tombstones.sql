-- ============================================================
-- Migration 027: consumer_groups_tombstones for V-207 replay guard (v1.5.1 #148)
-- ============================================================
-- Pre-v1.5.1 an admin could DELETE a consumer_group (last_cursor=N)
-- and then POST a new group with the same consumer_group_id. The new
-- row defaulted to last_cursor=0 (migration 021) and the connector
-- polling that group replayed every event since the dawn of the
-- outbox. Silent, catastrophic state drift at the downstream ERP if
-- the consumer was not strictly idempotent on event_id (the v1.5
-- wire contract does not require it).
--
-- v1.5.1 records a tombstone on DELETE. A later CREATE with the same
-- consumer_group_id checks the tombstone table; if a prior deletion
-- is recorded, the create fails 409 replay_would_skip_history unless
-- the admin explicitly submits acknowledge_replay=true. The tombstone
-- also carries the last_cursor_at_delete so the 409 response can
-- tell the admin exactly what gap the recreate would open.
--
-- Tombstones are UPSERTed on repeated delete cycles so the most
-- recent cursor always wins; a successful acknowledged-replay
-- create clears the row so subsequent fresh creates with an
-- unrelated ID do not trip the guard.
-- ============================================================

CREATE TABLE IF NOT EXISTS consumer_groups_tombstones (
    consumer_group_id      VARCHAR(64)  PRIMARY KEY,
    last_cursor_at_delete  BIGINT       NOT NULL DEFAULT 0,
    connector_id           VARCHAR(64),
    deleted_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    deleted_by             VARCHAR(100)
);
