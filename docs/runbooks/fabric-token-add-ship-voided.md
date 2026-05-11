# Fabric polling token: add ship.voided to event_types

Audience: operators rolling out the v1.9 dockd integration.

Scope: a one-time admin-side token update so the Fabric polling consumer receives `ship.voided/1` events from the outbox. Without this update, voids accumulate in `integration_events` and Fabric's downstream inventory reconciliation drifts silently from Sentry's truth.

## Why this matters

v1.9 adds the dockd `POST /api/v1/dockd/orders/<so>/void-ship` route. Every successful void emits one `ship.voided/1` event onto the integration_events outbox so consumers can reverse the corresponding PO line update or marketplace inventory adjustment from the original `ship.confirmed`.

The polling endpoint (`GET /api/v1/events`) filters events by the caller token's `event_types` array. A token without `ship.voided` in its scope sees the rows persist on the database side but never receives them at the wire. The token is the gate, not the schema registry.

Sentry registers `ship.voided` in `V150_CATALOG` at v1.9.0 boot. That makes it issuable on new tokens and visible in the admin UI's checkbox group. **Existing tokens are not auto-updated**; the operator has to add the new event_type explicitly.

## When to do this

Once, after the v1.9.0 deploy lands and before the first production void.

If you forget: voids are persisted (audit trail, fulfillment row, integration_events row) but Fabric does not reconcile them. The fix is the same procedure below; in-flight voided events remain in the outbox and Fabric picks them up on the next poll after the token's scope is widened, subject to the 90-day delivery retention window.

## Procedure (admin UI)

1. Open the admin Tokens page.
2. Find the Fabric polling token (token_name typically "fabric-poll" or matching the connector_id wired in `connector_credentials`).
3. Click Edit.
4. In the event_types checkbox group, tick `ship.voided` alongside the existing entries (`receipt.completed`, `ship.confirmed`, etc.).
5. Save. The token's `event_types` array now includes `ship.voided`.
6. Cross-worker token-cache invalidation propagates the change in <1s via the existing pubsub + Postgres LISTEN path (`services/token_cache.py`); no restart required.
7. Verify with one poll:
   ```bash
   curl -H "X-WMS-Token: $FABRIC_TOKEN" \
        "https://sentry.avidmax.com/api/v1/events/types"
   ```
   `ship.voided` should appear in the returned types.

## Procedure (SQL fallback)

For headless / scripted deployments:

```sql
UPDATE wms_tokens
   SET event_types = array_append(event_types, 'ship.voided')
 WHERE token_name = 'fabric-poll'
   AND NOT 'ship.voided' = ANY(event_types);
```

The `array_append` is idempotent under the `NOT 'ship.voided' = ANY(...)` guard so a re-run is safe. The audit_log hash chain extends automatically; the V-208 / V-221 chain trigger on `wms_tokens` updates picks up the row mutation.

After the UPDATE, prod's per-worker token cache picks up the change either at the next 60s TTL refresh OR via the cross-worker invalidation publish that the admin endpoint emits on update. SQL-only updates do NOT publish on the channel (the publish lives in the admin route handler), so wait one TTL window or restart the API workers if you want the change immediate.

## Verification on the wire

After the scope update lands, drive a test void through dockd and watch for the event:

```bash
# 1. From dockd, void any SHIPPED test order.
# 2. From your monitoring host, poll Sentry as Fabric:
curl -H "X-WMS-Token: $FABRIC_TOKEN" \
     "https://sentry.avidmax.com/api/v1/events?after=$LAST_CURSOR"
```

The response should include a row with `event_type: "ship.voided"`, `aggregate_type: "sales_order"`, and a payload matching `docs/api/dockd-openapi.yaml`'s `ship.voided/1` schema.

## Rollback

There is no rollback to perform. Removing `ship.voided` from the token's `event_types` would drop the consumer back to the v1.8 behavior (voids in the outbox, no delivery). Don't do this without a good reason; the downstream inventory drift is silent.
