-- ============================================================
-- Migration 047: serialize audit_log_chain_hash + log_id allocation (v1.7.0 #271)
-- ============================================================
-- Pre-mig-047 the V-025 chain trigger ran
--   SELECT row_hash ... ORDER BY log_id DESC LIMIT 1
-- with no serialization. Two concurrent inserts both read the same
-- prev_hash and forked the chain.
--
-- Two iterations on this fix were tried and discarded:
--   1. pg_advisory_xact_lock: under READ COMMITTED a PL/pgSQL trigger's
--      SELECT inherits the parent INSERT statement's snapshot taken
--      BEFORE the lock-wait, so even with lock-serialized entry the
--      SELECT read stale prev_hash.
--   2. SELECT FOR UPDATE on a sentinel row: lock + EvalPlanQual
--      serialized the sentinel read correctly, but the audit_log
--      log_id is assigned by the BIGSERIAL DEFAULT *before* the
--      BEFORE INSERT trigger fires. Two concurrent transactions
--      could call nextval() and get log_id=1 and log_id=2, then
--      execute their triggers in REVERSE order under the lock --
--      log_id=2's trigger ran first with prev='\x00', log_id=1's
--      trigger ran second with prev=log_id=2's row_hash. Chain held
--      by trigger-execution-order but NOT by log_id-order.
--
-- The actual fix moves log_id allocation INSIDE the lock-protected
-- critical section. The audit_log.log_id column drops its sequence
-- DEFAULT; the trigger assigns NEW.log_id := nextval(...) after
-- acquiring the table lock. log_id values still come from the
-- sequence (still unique, still monotonic) but their assignment
-- order matches trigger-execution order, so strict-by-log_id chain
-- integrity holds.
--
-- LOCK TABLE ... IN EXCLUSIVE MODE on audit_log_chain_head is the
-- serialization primitive. EXCLUSIVE doesn't conflict with itself
-- (it's reentrant within a transaction), so a multi-row INSERT in
-- one transaction works. It blocks every other transaction's
-- attempted EXCLUSIVE acquire until commit. SELECT and concurrent
-- READ access on the sentinel from outside the trigger are
-- unaffected because they take ACCESS SHARE which doesn't conflict
-- with EXCLUSIVE in the way that matters here -- but no code reads
-- the sentinel outside the trigger.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS audit_log_chain_head (
    singleton  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    row_hash   BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO audit_log_chain_head (singleton, row_hash)
SELECT TRUE, COALESCE(
    (SELECT row_hash FROM audit_log ORDER BY log_id DESC LIMIT 1),
    '\x00'::bytea
)
ON CONFLICT (singleton) DO UPDATE
   SET row_hash = COALESCE(
        (SELECT row_hash FROM audit_log ORDER BY log_id DESC LIMIT 1),
        '\x00'::bytea
   );

-- log_id keeps its underlying sequence, but the column-level DEFAULT
-- is dropped so concurrent transactions can't pre-allocate log_id
-- values out of trigger-execution order. The trigger assigns
-- NEW.log_id := nextval(...) after acquiring the lock.
ALTER TABLE audit_log ALTER COLUMN log_id DROP DEFAULT;

CREATE OR REPLACE FUNCTION audit_log_chain_hash() RETURNS TRIGGER AS $$
DECLARE
    prev BYTEA;
    payload TEXT;
BEGIN
    -- Serialize the entire critical section: log_id allocation +
    -- prev_hash read + row_hash compute + sentinel update. Ensures
    -- log_id-order matches trigger-execution-order, so strict-by-
    -- log_id chain holds.
    LOCK TABLE audit_log_chain_head IN EXCLUSIVE MODE;
    NEW.log_id := nextval('audit_log_log_id_seq');
    SELECT row_hash INTO prev FROM audit_log_chain_head
     WHERE singleton = TRUE;
    NEW.prev_hash := COALESCE(prev, '\x00'::bytea);
    payload := COALESCE(NEW.action_type, '') || '|' ||
               COALESCE(NEW.entity_type, '') || '|' ||
               COALESCE(NEW.entity_id::text, '') || '|' ||
               COALESCE(NEW.user_id, '') || '|' ||
               COALESCE(NEW.warehouse_id::text, '') || '|' ||
               COALESCE(NEW.details::text, '') || '|' ||
               COALESCE(NEW.created_at::text, NOW()::text);
    NEW.row_hash := digest(NEW.prev_hash || payload::bytea, 'sha256');
    UPDATE audit_log_chain_head
       SET row_hash = NEW.row_hash, updated_at = NOW()
     WHERE singleton = TRUE;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMIT;
