-- 012: Cycle count pending audit support
-- Adds status to inventory_adjustments (PENDING/APPROVED/REJECTED)
-- Adds unexpected flag to cycle_count_lines for items found during count
-- that were not in the original snapshot

ALTER TABLE inventory_adjustments
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'PENDING';

ALTER TABLE cycle_count_lines
    ADD COLUMN IF NOT EXISTS unexpected BOOLEAN DEFAULT FALSE;
