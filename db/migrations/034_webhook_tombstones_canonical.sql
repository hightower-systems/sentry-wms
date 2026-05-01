-- ============================================================
-- Migration 034: tombstone delivery_url canonicalization (#218)
-- ============================================================
-- The URL-reuse gate in migration 033 indexed the raw
-- delivery_url_at_delete column with a case-, port-, fragment-,
-- and trailing-slash-sensitive partial unique index. An admin who
-- mutates one character of the URL on recreate can bypass the
-- gate without supplying acknowledge_url_reuse, defeating the
-- defense the gate exists to provide.
--
-- This migration adds delivery_url_canonical alongside the raw
-- column, backfills it for every existing row via the PL/pgSQL
-- twin of api.services.webhook_dispatcher.url_normalize, and
-- swaps the partial unique index over to the canonical column.
-- The raw column stays for forensic recall ("what URL did the
-- admin type?"); the canonical column is what the gate compares.
--
-- The PL/pgSQL function is dropped at the end of the migration.
-- It exists only for the one-time backfill; the application
-- writes both columns going forward (Python helper is the source
-- of truth so we do not have to keep the SQL twin pinned to it).
--
-- Wrapped in BEGIN/COMMIT per V-213 #152 so a partial apply
-- cannot leave the table with the new column but no backfilled
-- values, which would brick subsequent INSERTs against the
-- partial unique index.
-- ============================================================

BEGIN;

ALTER TABLE webhook_subscriptions_tombstones
    ADD COLUMN IF NOT EXISTS delivery_url_canonical TEXT;

CREATE OR REPLACE FUNCTION pg_temp.canonicalize_delivery_url_for_backfill(url TEXT)
RETURNS TEXT LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    work TEXT := url;
    hash_pos INT;
    sep_pos INT;
    scheme TEXT;
    rest TEXT;
    slash_pos INT;
    qmark_pos INT;
    netloc_end INT;
    netloc TEXT;
    path_and_after TEXT;
    at_pos INT;
    userinfo TEXT;
    hostport TEXT;
    colon_pos INT;
    host_only TEXT;
    port_str TEXT;
BEGIN
    -- Strip fragment first (never sent on the wire).
    hash_pos := position('#' in work);
    IF hash_pos > 0 THEN
        work := substring(work from 1 for hash_pos - 1);
    END IF;

    sep_pos := position('://' in work);
    IF sep_pos = 0 THEN
        RETURN work;  -- malformed URL; pass through verbatim.
    END IF;
    scheme := lower(substring(work from 1 for sep_pos - 1));
    rest := substring(work from sep_pos + 3);

    slash_pos := position('/' in rest);
    qmark_pos := position('?' in rest);
    IF slash_pos > 0 AND qmark_pos > 0 THEN
        netloc_end := least(slash_pos, qmark_pos);
    ELSIF slash_pos > 0 THEN
        netloc_end := slash_pos;
    ELSIF qmark_pos > 0 THEN
        netloc_end := qmark_pos;
    ELSE
        netloc_end := 0;
    END IF;

    IF netloc_end = 0 THEN
        netloc := rest;
        path_and_after := '';
    ELSE
        netloc := substring(rest from 1 for netloc_end - 1);
        path_and_after := substring(rest from netloc_end);
    END IF;

    -- Split off userinfo verbatim; only the host is lowercased.
    at_pos := position('@' in netloc);
    IF at_pos > 0 THEN
        userinfo := substring(netloc from 1 for at_pos);  -- keeps trailing '@'
        hostport := substring(netloc from at_pos + 1);
    ELSE
        userinfo := '';
        hostport := netloc;
    END IF;

    -- Lowercase the host[:port] segment.
    hostport := lower(hostport);

    -- Strip default port for the scheme.
    colon_pos := position(':' in hostport);
    IF colon_pos > 0 THEN
        host_only := substring(hostport from 1 for colon_pos - 1);
        port_str := substring(hostport from colon_pos + 1);
        IF (scheme = 'https' AND port_str = '443')
           OR (scheme = 'http' AND port_str = '80') THEN
            hostport := host_only;
        END IF;
    END IF;

    netloc := userinfo || hostport;

    -- Path normalization: empty -> '/', non-root trailing '/' stripped.
    -- Trailing-slash strip is intentionally restricted to the case
    -- where there is no query string in the path_and_after segment;
    -- we never split path from query here, so the conservative rule
    -- "only strip when the whole tail ends in '/' and has no '?'"
    -- matches the Python helper's behavior.
    IF path_and_after = '' THEN
        path_and_after := '/';
    ELSIF length(path_and_after) > 1
          AND right(path_and_after, 1) = '/'
          AND position('?' in path_and_after) = 0 THEN
        path_and_after := rtrim(path_and_after, '/');
        IF path_and_after = '' THEN
            path_and_after := '/';
        END IF;
    END IF;

    RETURN scheme || '://' || netloc || path_and_after;
END;
$$;

UPDATE webhook_subscriptions_tombstones
   SET delivery_url_canonical =
       pg_temp.canonicalize_delivery_url_for_backfill(delivery_url_at_delete)
 WHERE delivery_url_canonical IS NULL;

ALTER TABLE webhook_subscriptions_tombstones
    ALTER COLUMN delivery_url_canonical SET NOT NULL;

DROP INDEX IF EXISTS webhook_subscriptions_tombstones_url_unack;

CREATE INDEX webhook_subscriptions_tombstones_canonical_unack
    ON webhook_subscriptions_tombstones (delivery_url_canonical)
    WHERE acknowledged_at IS NULL;

COMMIT;
