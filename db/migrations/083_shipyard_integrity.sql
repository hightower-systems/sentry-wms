-- 083: make shipyard effective windows real and close import integrity gaps.
--
-- The first slice created UNIQUE indexes over status='ACTIVE'. That prevented
-- two future non-overlapping rules from coexisting and made a configured
-- effective_to effectively meaningless. Replace those indexes with temporal
-- exclusion constraints so adjacent future windows are schedulable.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

DROP INDEX IF EXISTS uq_shipyard_project_binding_active;
DROP INDEX IF EXISTS uq_shipyard_zone_ship_primary;
DROP INDEX IF EXISTS uq_shipyard_zone_ship_bin;

ALTER TABLE shipyard_project_bindings
    DROP CONSTRAINT IF EXISTS shipyard_project_binding_no_overlap;

ALTER TABLE shipyard_project_bindings
    ADD CONSTRAINT shipyard_project_binding_no_overlap
    EXCLUDE USING gist (
        ship_no WITH =,
        engineering_no WITH =,
        production_drawing_no WITH =,
        manufacturer_name WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    )
    WHERE (status = 'ACTIVE');

ALTER TABLE shipyard_zone_ship_mappings
    DROP CONSTRAINT IF EXISTS shipyard_zone_ship_primary_no_overlap,
    DROP CONSTRAINT IF EXISTS shipyard_zone_ship_bin_no_overlap;

ALTER TABLE shipyard_zone_ship_mappings
    ADD CONSTRAINT shipyard_zone_ship_primary_no_overlap
    EXCLUDE USING gist (
        ship_no WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    )
    WHERE (status = 'ACTIVE' AND is_primary);

ALTER TABLE shipyard_zone_ship_mappings
    ADD CONSTRAINT shipyard_zone_ship_bin_no_overlap
    EXCLUDE USING gist (
        bin_id WITH =,
        ship_no WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    )
    WHERE (status = 'ACTIVE');

DROP INDEX IF EXISTS uq_shipyard_inbound_manifest_file;

CREATE UNIQUE INDEX uq_shipyard_inbound_manifest_file
    ON shipyard_inbound_manifests (warehouse_id, source_file_sha256)
    WHERE source_file_sha256 IS NOT NULL
      AND status <> 'VOID';

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_pallet_qr_payload_sha256
    ON shipyard_pallet_units (qr_payload_sha256);

CREATE TABLE IF NOT EXISTS shipyard_manifest_files (
    manifest_id     UUID PRIMARY KEY
                        REFERENCES shipyard_inbound_manifests(manifest_id)
                        ON DELETE CASCADE,
    file_name       VARCHAR(255) NOT NULL,
    content_type    VARCHAR(128) NOT NULL,
    file_size       BIGINT NOT NULL,
    file_sha256     CHAR(64) NOT NULL,
    file_bytes      BYTEA NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT shipyard_manifest_file_size
        CHECK (file_size > 0 AND file_size <= 10485760),
    CONSTRAINT shipyard_manifest_file_sha256
        CHECK (length(file_sha256) = 64)
);

COMMIT;
