-- avid-overhaul-mk1: fix audit_log hash chain trigger so multi-line
-- audit details no longer trip bytea escape parsing.
--
-- The v1.7.0 hash chain (mig 047) builds its hash input by serialising
-- the row's fields to TEXT and casting that to BYTEA via `payload::bytea`.
-- That cast uses Postgres bytea ESCAPE input parsing, which treats a
-- backslash as the start of an octal escape (\nnn) or a quoted literal
-- (\\). When NEW.details is JSONB containing a string with embedded
-- newlines, NEW.details::text re-serialises the JSON and emits a real
-- "\n" two-character sequence inside the result. The bytea escape
-- parser sees `\n`, which is neither a valid octal triple nor `\\`,
-- and raises:
--
--     invalid input syntax for type bytea
--
-- The audit_log INSERT lands inside the same transaction as the
-- triggering UPDATE, so the whole operation rolls back. From the
-- user's perspective, saving an SO memo with line breaks fails with
-- a generic 500 even though the new memo value itself is valid.
--
-- convert_to(text, 'UTF8') encodes the text as UTF-8 bytes verbatim
-- without escape parsing, so any TEXT input round-trips into BYTEA
-- safely regardless of backslash content. The hash output stays
-- byte-identical to the existing chain for ASCII-clean rows (UTF-8
-- of ASCII is the same bytes), so historical row_hash values still
-- verify against the new function.

CREATE OR REPLACE FUNCTION audit_log_chain_hash() RETURNS TRIGGER AS $$
DECLARE
    prev BYTEA;
    payload TEXT;
BEGIN
    -- v1.7.0 #271: serialize the entire critical section (log_id
    -- allocation + prev_hash read + row_hash compute + sentinel
    -- update). EXCLUSIVE table lock blocks other writers; nextval
    -- inside the lock guarantees log_id-order matches trigger-
    -- execution-order so the strict-by-log_id chain holds.
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
    -- avid-overhaul-mk1: convert_to instead of ::bytea so backslash
    -- escapes in the serialised JSON do not trip bytea escape parsing.
    NEW.row_hash := digest(NEW.prev_hash || convert_to(payload, 'UTF8'), 'sha256');
    UPDATE audit_log_chain_head
       SET row_hash = NEW.row_hash, updated_at = NOW()
     WHERE singleton = TRUE;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- verify_audit_log_chain has to use the same byte encoding as the
-- trigger or it will reject every chain row written after the fix.
-- ASCII content hashes identically under both ::bytea (escape) and
-- convert_to (UTF-8), so pre-fix rows still verify cleanly.

CREATE OR REPLACE FUNCTION verify_audit_log_chain() RETURNS BIGINT AS $$
DECLARE
    prev BYTEA := '\x00'::bytea;
    r RECORD;
    computed BYTEA;
    payload TEXT;
BEGIN
    FOR r IN SELECT * FROM audit_log ORDER BY log_id ASC LOOP
        IF r.prev_hash IS DISTINCT FROM prev THEN
            RETURN r.log_id;
        END IF;
        payload := COALESCE(r.action_type, '') || '|' ||
                   COALESCE(r.entity_type, '') || '|' ||
                   COALESCE(r.entity_id::text, '') || '|' ||
                   COALESCE(r.user_id, '') || '|' ||
                   COALESCE(r.warehouse_id::text, '') || '|' ||
                   COALESCE(r.details::text, '') || '|' ||
                   COALESCE(r.created_at::text, '');
        computed := digest(r.prev_hash || convert_to(payload, 'UTF8'), 'sha256');
        IF computed IS DISTINCT FROM r.row_hash THEN
            RETURN r.log_id;
        END IF;
        prev := r.row_hash;
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
