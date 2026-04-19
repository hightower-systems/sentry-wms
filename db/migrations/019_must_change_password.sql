-- ============================================================
-- Migration 019: must_change_password flag (v1.4.1)
-- ============================================================
-- Adds a must_change_password BOOLEAN to users. Fresh-seed installs
-- set this to TRUE for the default admin so the first login is
-- forced into the change-password flow before any other route is
-- accessible. Existing users get the column default FALSE on
-- ALTER -- they are never force-flagged by this migration.
--
-- The flag is cleared in the same transaction as a successful
-- change-password call (see api/routes/auth.py). Auth middleware
-- reads the flag on every request and blocks all but three
-- endpoints (/api/auth/me, /api/auth/change-password,
-- /api/auth/logout) with 403 password_change_required while set.
-- ============================================================

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
