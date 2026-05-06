-- v1.7.0 #274: defense-in-depth for token revocation cache invalidation.
--
-- Pre-#274, only the Flask admin handler `revoke_token` invalidated
-- token caches across workers (via the Redis pubsub publisher in
-- services.token_cache.invalidate). A direct DB UPDATE to
-- wms_tokens.revoked_at -- from psql, an ad-hoc maintenance script,
-- an emergency runbook, or any caller bypassing the admin API --
-- skipped that path entirely, leaving every gunicorn worker
-- authenticating the compromised token for up to the 60s TTL.
--
-- This trigger publishes pg_notify('wms_token_revocations', token_id)
-- on every NULL -> NOT NULL transition of revoked_at (or a change of
-- the timestamp) so the LISTEN subscriber in services.token_cache
-- evicts the token across workers regardless of who issued the
-- UPDATE.
--
-- The admin API path also fires this trigger; the local invalidate
-- is idempotent so the double-fire (Redis publish + Postgres NOTIFY)
-- is harmless.

CREATE OR REPLACE FUNCTION wms_tokens_revocation_notify()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- Fire only when revoked_at goes from NULL to NOT NULL, or changes
    -- between two non-NULL timestamps (re-revocation). A no-op UPDATE
    -- (revoked_at unchanged) does not emit; the trigger declares
    -- AFTER UPDATE OF revoked_at so a row update that doesn't touch
    -- revoked_at never reaches this function.
    IF NEW.revoked_at IS NOT NULL
       AND (OLD.revoked_at IS NULL OR OLD.revoked_at <> NEW.revoked_at)
    THEN
        -- v1.7.0 #278: keep the `status` column in lock-step with
        -- revoked_at when a direct-DB write sets revoked_at without
        -- updating status. Pre-fix, gate 17 caught the gap: an
        -- operator running `UPDATE wms_tokens SET revoked_at = NOW()`
        -- against a row whose status was 'active' produced a token
        -- that the auth middleware (which gated only on
        -- status='active') still authenticated, even after the
        -- pg_notify trigger evicted the cache. The auth check is
        -- tightened separately (auth_middleware.py); this trigger
        -- closes the schema half so the on-disk row tells a
        -- consistent story to investigators.
        --
        -- Idempotent: only fires the secondary UPDATE when status
        -- is not already 'revoked'. The trigger declares AFTER
        -- UPDATE OF revoked_at so the inner UPDATE on `status`
        -- alone does not re-enter this function (column-list filter).
        IF NEW.status IS DISTINCT FROM 'revoked' THEN
            UPDATE wms_tokens
               SET status = 'revoked'
             WHERE token_id = NEW.token_id;
        END IF;
        PERFORM pg_notify(
            'wms_token_revocations',
            NEW.token_id::text
        );
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tr_wms_tokens_revocation_notify ON wms_tokens;
CREATE TRIGGER tr_wms_tokens_revocation_notify
    AFTER UPDATE OF revoked_at ON wms_tokens
    FOR EACH ROW
    EXECUTE FUNCTION wms_tokens_revocation_notify();
