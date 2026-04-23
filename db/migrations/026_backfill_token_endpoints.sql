-- ============================================================
-- Migration 026: backfill wms_tokens.endpoints for pre-v1.5.1 tokens (V-200 #140)
-- ============================================================
-- v1.5.0 shipped wms_tokens.endpoints as a scope column but the
-- @require_wms_token decorator never consulted it (V-200, #140).
-- Admins could issue tokens with endpoints=[] or any-slug and all
-- /api/v1/* routes remained reachable. v1.5.1 closes the gap by
-- enforcing the slug list at request time with "empty = deny"
-- semantics (matching warehouse_ids / event_types per plan Decision S).
--
-- This migration keeps pre-v1.5.1 tokens working after upgrade by
-- populating any empty endpoints array with the full v1 slug set.
-- Tokens that already had an explicit slug list keep it unchanged.
-- Fresh tokens issued after v1.5.1 must supply a non-empty list
-- (admin_tokens.py + CreateTokenRequest reject empty at the API
-- layer).
--
-- The DEFAULT on the column stays '{}' so the API-layer validation
-- is the single source of truth for "what endpoints are valid";
-- the DB does not enumerate them.
-- ============================================================

UPDATE wms_tokens
   SET endpoints = ARRAY[
         'events.poll',
         'events.ack',
         'events.types',
         'events.schema',
         'snapshot.inventory'
       ]::TEXT[]
 WHERE endpoints = '{}'::TEXT[];
