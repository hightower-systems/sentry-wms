-- ============================================================
-- Migration 009: Mobile App Support
-- ============================================================
-- Adds allowed_functions column to users table
-- Creates app_settings table for configurable settings
-- ============================================================

-- Add allowed_functions column to users table
ALTER TABLE users ADD COLUMN allowed_functions TEXT[] DEFAULT '{}';

-- App settings table
CREATE TABLE app_settings (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) UNIQUE NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Default session timeout
INSERT INTO app_settings (key, value) VALUES ('session_timeout_hours', '8');
