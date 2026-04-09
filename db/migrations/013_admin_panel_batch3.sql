-- Migration 013: Admin panel batch 3
-- Multi-warehouse users, simplified roles, item archiving

-- Add warehouse_ids array for multi-warehouse assignment
ALTER TABLE users ADD COLUMN IF NOT EXISTS warehouse_ids INT[] DEFAULT '{}';

-- Migrate existing warehouse_id values into warehouse_ids
UPDATE users SET warehouse_ids = ARRAY[warehouse_id]
WHERE warehouse_id IS NOT NULL AND (warehouse_ids IS NULL OR warehouse_ids = '{}');

-- Simplify roles: collapse MANAGER/PICKER/RECEIVER/PACKER → USER
UPDATE users SET role = 'USER' WHERE role NOT IN ('ADMIN', 'USER');

-- Give all existing users full mobile module access if empty
UPDATE users SET allowed_functions = '{receive,putaway,pick,pack,ship,count,transfer}'
WHERE allowed_functions IS NULL OR allowed_functions = '{}';
