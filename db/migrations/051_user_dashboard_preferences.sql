-- ============================================================
-- Migration 051: user_dashboard_preferences + audit_log dashboard
--                covering index + warehouses.timezone (v1.8.0 #283)
-- ============================================================
-- Three additions in one migration, all serving the v1.8.0
-- productivity dashboard:
--
--   1. user_dashboard_preferences -- per-user override storage for
--      chart_order / default_range / default_view. Defaults are
--      applied at read time when no row exists; this table is
--      override storage, not a settings record.
--
--   2. ix_audit_log_dashboard -- compound covering index on
--      audit_log(action_type, created_at, user_id, warehouse_id)
--      INCLUDE (entity_id, details). The productivity-dashboard
--      aggregation query reads from audit_log (canonical "who did
--      what"), not integration_events; this index keeps cold-cache
--      p95 under 500ms over a 30-day window at ~100k audit rows.
--      audit_log is append-only at AvidMax scale (~40k rows/month)
--      so the index write overhead is acceptable. Existing
--      ix_audit_log_action is kept (overlapping but not strict
--      superset).
--
--   3. warehouses.timezone -- "Today" / "Yesterday" range mapping
--      is warehouse-local, not UTC; the column avoids a per-call
--      lookup against operator config. Default America/Denver
--      matches the AvidMax baseline; per-warehouse override via
--      operator UPDATE in v1.8 (no admin UI in this release).
--
-- v1.8.0 migration discipline: SET lock_timeout / statement_timeout
-- at the top so a bad migration fails fast. BEGIN/COMMIT-wrapped
-- per V-213.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

CREATE TABLE IF NOT EXISTS user_dashboard_preferences (
    user_id          INT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    chart_order      JSONB NOT NULL DEFAULT '["picking","packing","shipped","received_skus","putaway_skus"]'::jsonb,
    default_range    VARCHAR(16) NOT NULL DEFAULT 'today'
                       CHECK (default_range IN ('today','yesterday','last_7d','last_30d','custom')),
    default_view     VARCHAR(8) NOT NULL DEFAULT 'charts'
                       CHECK (default_view IN ('charts','table')),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Compound covering index for the productivity aggregation query.
-- Pattern:
--   WHERE created_at BETWEEN :s AND :e
--     AND action_type = ANY(:actions)
--     AND warehouse_id = :w
--   GROUP BY user_id, action_type
-- INCLUDE clause covers the projection so the planner can answer
-- from index pages alone.
CREATE INDEX IF NOT EXISTS ix_audit_log_dashboard
    ON audit_log(action_type, created_at, user_id, warehouse_id)
    INCLUDE (entity_id, details);

ALTER TABLE warehouses
    ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'America/Denver';

COMMIT;
