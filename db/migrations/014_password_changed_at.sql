-- Migration 014: Add password_changed_at for token invalidation (M1/L10)
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;
