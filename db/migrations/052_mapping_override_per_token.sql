-- ============================================================
-- Migration 052: wms_tokens.mapping_overrides per token (v1.8.0 #284)
-- ============================================================
-- Schema half of #270 (mapping_override semantics resolution:
-- Option B -- per-token static JSONB column).
--
-- The existing mapping_override BOOLEAN capability flag (v1.7
-- mig 037) stays as the gate; per-token overrides apply only when
-- both the boolean is TRUE and the JSONB is non-empty. Backward
-- compatible: existing tokens have '{}' after the migration runs
-- and the inbound handler skips the override path when the flag is
-- FALSE. Token-cache + handler wiring + admin UI editor lands in
-- Pass 3 of v1.8.0; this migration ships only the column.
--
-- Per-request body overrides (Option A) and per-mapping-document
-- escape hatches (Option C) remain deferred to v1.x if real demand
-- surfaces.
--
-- v1.8.0 migration discipline: SET lock_timeout / statement_timeout
-- at the top so a bad migration fails fast. BEGIN/COMMIT-wrapped
-- per V-213.
-- ============================================================

SET lock_timeout = '5s';
SET statement_timeout = '60s';

BEGIN;

ALTER TABLE wms_tokens
    ADD COLUMN IF NOT EXISTS mapping_overrides JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMIT;
