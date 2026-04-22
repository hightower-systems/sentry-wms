-- ============================================================
-- Migration 022: credential_type on connector_credentials (v1.5.0)
-- ============================================================
-- Adds a credential_type column so v2+ outbound credential flavours
-- (outbound_oauth, outbound_api_key, outbound_bearer) can live in the
-- same table as today's connector_api_key rows without a schema
-- change later. Every existing row gets 'connector_api_key' via the
-- DEFAULT; the NOT NULL constraint holds without backfill.
--
-- NOTE: inbound tokens do NOT live in connector_credentials. They get
-- their own hash-only table (wms_tokens) in migration 023. There is
-- intentionally no 'inbound_token' value in this enum - separate
-- table, separate lifecycle.
-- ============================================================

ALTER TABLE connector_credentials
    ADD COLUMN IF NOT EXISTS credential_type VARCHAR(32) NOT NULL DEFAULT 'connector_api_key';
