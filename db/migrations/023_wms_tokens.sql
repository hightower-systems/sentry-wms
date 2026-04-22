-- ============================================================
-- Migration 023: wms_tokens inbound-token vault (v1.5.0)
-- ============================================================
-- Inbound API tokens for X-WMS-Token auth against /api/v1/events* and
-- /api/v1/snapshot/*. Hash-only storage per Decision P: plaintext is
-- shown exactly once at issuance and discarded server-side. Lost
-- plaintext = rotate. Same pattern as GitHub PATs, Stripe keys,
-- AWS access keys.
--
-- token_hash = SHA256(SENTRY_TOKEN_PEPPER || plaintext).hexdigest()
-- per Decision Q. SENTRY_TOKEN_PEPPER is an env-only secret; the app
-- fails to boot without it (see #128 decorator). Rotating the pepper
-- is an emergency-only control that invalidates every token at once.
--
-- Scope columns are typed arrays (Decision S), not JSONB: the
-- polling decorator reads warehouse_ids / event_types / endpoints
-- directly, and future GIN indexing on any of them is free.
--
-- expires_at defaults to +1 year (Decision R) so a forgotten token
-- still eventually stops working. Admin can override at issuance.
-- ============================================================

CREATE TABLE IF NOT EXISTS wms_tokens (
    token_id       BIGSERIAL     PRIMARY KEY,
    token_name     VARCHAR(128)  NOT NULL,
    token_hash     CHAR(64)      UNIQUE NOT NULL,
    warehouse_ids  BIGINT[]      NOT NULL DEFAULT '{}',
    event_types    TEXT[]        NOT NULL DEFAULT '{}',
    endpoints      TEXT[]        NOT NULL DEFAULT '{}',
    connector_id   VARCHAR(64)   REFERENCES connectors(connector_id),
    status         VARCHAR(16)   NOT NULL DEFAULT 'active',
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    rotated_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    expires_at     TIMESTAMPTZ   NOT NULL DEFAULT (NOW() + INTERVAL '1 year'),
    revoked_at     TIMESTAMPTZ,
    last_used_at   TIMESTAMPTZ
);

-- Support the admin-panel rotation-age badge: "SELECT token_id,
-- token_name, status, rotated_at FROM wms_tokens WHERE status='active'
-- ORDER BY rotated_at". The (status, rotated_at) index covers both
-- the filter and the sort in one scan.
CREATE INDEX IF NOT EXISTS wms_tokens_status_rotated
    ON wms_tokens (status, rotated_at);
