# Admin Panel

The admin panel is a React web app at `http://localhost:8080` for warehouse managers to monitor operations and configure the system. Requires ADMIN role. The production build is served by nginx; a development overlay (`docker-compose.dev.yml`) restores the Vite dev-server on port 3000 with hot reload.

---

## Dashboard

<!-- TODO: Add screenshot -->

The home page shows a real-time pipeline overview:

- **Pipeline bar** - visual flow from Receiving to Put-Away to Picking to Packing to Shipping
- **Open orders needing action** - POs awaiting receipt, SOs ready to pick
- **Low stock alerts** - items below reorder point
- **Short picks (7 day)** - recent short pick events with SKU, bin, and shortage details
- **Recent activity** - last 10 audit log entries
- **Inbound POs** - purchase orders with receipt status

All stats filter by the warehouse selected in the header dropdown.

---

## Inventory

<!-- TODO: Add screenshot -->

Full inventory view showing stock by bin location.

- Search by SKU or item name
- Sort by any column (click headers)
- Columns: SKU, item name, bin code, zone, quantity on hand, quantity allocated, quantity available, last counted
- Filter by warehouse via header dropdown
- Paginated

---

## Items

<!-- TODO: Add screenshot -->

Product catalog management.

- **Search** by SKU, name, or UPC
- **Filter** by Active, Archived, or All
- **Create** new items with SKU, name, UPC, category, weight, default bin
- **Edit** any item field
- **Archive/Restore** soft delete toggle
- **Delete** hard delete (blocked if inventory or order history exists)
- **Detail view** shows inventory locations across all bins and preferred bin assignments

---

## Purchase Orders

<!-- TODO: Add screenshot -->

- List all POs with status tags (OPEN, PARTIAL, RECEIVED, CLOSED)
- **Filter** by status
- **Create PO** with PO number, vendor, expected date, and line items (item ID + quantity)
- **Detail modal** shows ordered vs received quantities per line
- **Close PO** action

---

## Sales Orders

<!-- TODO: Add screenshot -->

- List all SOs with status, customer info, carrier, and tracking
- **Filter** by status (OPEN, PICKING, PICKED, PACKED, SHIPPED, CANCELLED)
- **Create SO** with SO number, customer name/phone/address, ship method, and line items
- **Detail modal** shows fulfillment progress per line (ordered, allocated, picked, packed, shipped)
- **Cancel SO** releases allocated inventory

---

## Users

<!-- TODO: Add screenshot -->

- List all user accounts with role, warehouse assignments, and active status
- **Create user** with username, password, full name, role (ADMIN or USER)
- **Warehouse assignment** - multi-select warehouses the user can access
- **Module access** - checkboxes for mobile functions (Pick, Pack, Ship, Receive, Put-Away, Count, Transfer)
- **Edit** any field including password reset
- **Delete** hard delete (cannot delete yourself or the last admin)

---

## Warehouses

<!-- TODO: Add screenshot -->

- List all warehouses with code, name, address, active status
- **Create** new warehouses
- **Edit** name and address
- **Delete** (blocked if warehouse has bins, zones, or inventory)

---

## Zones

<!-- TODO: Add screenshot -->

- List zones within the selected warehouse
- **Create** with zone code, name, and type
- **Edit** zone properties
- Zone types: STORAGE, RECEIVING, STAGING, SHIPPING, QUALITY, DAMAGE

---

## Bins

<!-- TODO: Add screenshot -->

- List all bin locations with code, barcode, type, zone, pick sequence
- **Create** with bin code, barcode, type, zone, and optional coordinates (aisle, row, level, position)
- **Edit** any field
- **Detail modal** shows current inventory contents with quantities

Bin types: Pickable, PickableStaging, Staging

---

## Preferred Bins

<!-- TODO: Add screenshot -->

Item-to-bin priority assignments used by the put-away suggestion engine.

- Search by SKU or item name
- Create new preferred bin assignments with priority ranking
- Edit priorities
- Delete assignments
- CSV export

---

## Cycle Count Approvals

<!-- TODO: Add screenshot -->

Review pending inventory adjustments from cycle counts.

- Grouped by cycle count / bin
- Per-item approve or reject buttons
- Approve All / Reject All per group
- Shows expected vs counted quantities and variance
- Separation of duties check (configurable in Settings)

---

## Adjustments

<!-- TODO: Add screenshot -->

Direct inventory add/remove with reason tracking.

- **ADD** - increase quantity in a specific bin
- **REMOVE** - decrease quantity from a bin
- Searchable bin and item dropdowns
- Reason text required
- Auto-approved (no approval workflow)
- Adjustment history table

---

## Inter-Warehouse Transfers

<!-- TODO: Add screenshot -->

Move inventory between warehouses.

- Select source warehouse, source bin, and item
- Select destination warehouse and destination bin
- Enter quantity
- Transfer history table with timestamps and user

---

## Imports

<!-- TODO: Add screenshot -->

Bulk import via CSV or JSON for four entity types:

- **Items** - SKU, name, UPC, category, weight
- **Bins** - bin code, barcode, type, zone, coordinates
- **Purchase Orders** - PO number, SKU, quantity, vendor
- **Sales Orders** - SO number, SKU, quantity, customer

Download template buttons provide sample CSV files. Max 5000 records per import.

---

## Audit Log

<!-- TODO: Add screenshot -->

Activity log for all warehouse operations.

- Filter by action type, user, and date range
- Columns: timestamp, action, entity type, entity name, username, warehouse, device
- Detail modal with resolved entity names (bin codes, SKUs, PO/SO numbers)

---

## Settings

<!-- TODO: Add screenshot -->

System configuration:

- **Warehouse** - edit name and address for the selected warehouse
- **Fulfillment Workflow**
    - Require packing before shipping (checkbox)
    - Default receiving bin (dropdown)
    - Allow over-receiving (checkbox)
- **Inventory**
    - Require separate approver for cycle count adjustments (checkbox)
- **Mobile App**
    - Show expected quantities during cycle counts (checkbox)
- **Manual Entry** - create POs and SOs directly (for standalone deployments)
- **About** - version number and repository link

All settings use a batch save with unsaved changes warning.

## Integrations

The Integrations page (sidebar -> System -> Integrations) is the home for
ERP and commerce connectors. Each registered connector appears as a
button; selecting one opens a credential form whose fields come from
`get_config_schema()`. Values are encrypted with `SENTRY_ENCRYPTION_KEY`
before they hit the database and are displayed back as `****`. The same
card shows a Sync Health panel with live indicators (green / yellow /
red) for each sync type, the last success timestamp, the last error
message, and a **Sync Now** button per type (disabled while a sync is
running). See the [Connectors](connectors.md) guide for the framework
internals and how to add your own.

---

## API Tokens (v1.5+)

<!-- TODO: Add screenshot -->

Manage `X-WMS-Token` credentials used by external systems to call Sentry's polling, snapshot, webhook, and inbound APIs.

- **List tokens** - token name, status (active / revoked), scope summary, last-used timestamp, expiration
- **Create token** opens an issuance modal:
    - **Token name** - operator-readable label (e.g. `acme-erp-prod`)
    - **Warehouse IDs** - multi-select; empty denies the token from every warehouse-scoped endpoint
    - **Endpoints** - multi-select slug list (`events.poll`, `events.ack`, `events.types`, `events.schema`, `snapshot.inventory`, plus the v1.7 inbound slugs); empty denies (V-200)
    - **Event types** - multi-select scope dimension for outbound polling
    - **Connector ID** - optional FK to a registered connector
    - **Source system** (v1.7+) - dropdown sourced from `inbound_source_systems_allowlist`; required when issuing an inbound token
    - **Inbound resources** (v1.7+) - multi-select from `sales_orders`, `items`, `customers`, `vendors`, `purchase_orders`; empty denies inbound POSTs (Decision-S)
    - **Mapping override capability** (v1.7+) - checkbox; reserved for v1.7.1, currently has no runtime effect (the v1.7.0 handler rejects requests with `mapping_overrides` regardless per #269)
    - **Expiration** - defaults to one year from issuance
- **One-shot plaintext** - the modal displays the plaintext token exactly once on creation; subsequent reads only show the SHA256 hash. Operators copy the plaintext into the consuming system's secret store.
- **Rotate** - flips the token hash and returns a fresh plaintext (one-shot reveal); the prior hash stops authenticating immediately.
- **Revoke** - flips status to `revoked` and stamps `revoked_at`. v1.7.0 (#274, #278) propagates the revocation across workers via `pg_notify` + LISTEN within sub-second latency, AND covers direct-DB revokes (`UPDATE wms_tokens SET revoked_at = NOW()`) the same way.
- **Delete** - hard delete after confirmation; preserved as forensic audit row in `wms_tokens_audit` (V-157 / #157 forensic trail).
- **audit_log** writes on every issuance / rotate / revoke / delete (V-208 / #141).

Cross-direction scope rule: a token's outbound and inbound surfaces are independent; an inbound-only token cannot reach the polling endpoints and vice versa. Cross-direction misuse returns 401 `cross_direction_scope_violation`. See [SECURITY.md](https://github.com/hightower-systems/sentry-wms/blob/main/SECURITY.md) for the full scope-enforcement matrix.

---

## Consumer Groups (v1.5+)

<!-- TODO: Add screenshot -->

Manage outbound polling consumer state. Each consumer group is a named cursor over `integration_events` with a token binding; the polling endpoint advances the group's cursor on every successful `events.ack` call.

- **List groups** - group name, bound token, current cursor (`last_event_id`), last advance timestamp, status
- **Create group** - group name + token binding + initial cursor position (default: tail)
- **Detail view** - cursor advance history, recent acks, audit-log entries from the binding's mutations
- **Pause / resume** - pauses the cursor without advancing on `ack` calls; useful for incident response when a downstream consumer is mid-incident and you want to stop the cursor from moving
- **Delete** - hard delete with V-207 tombstone gate. Recreating a deleted group's name returns 409 `consumer_group_recreate_blocked` until the operator acknowledges via `acknowledge_recreate=true`. Tombstones cover the replay-window blast radius.
- **audit_log** writes on every CRUD operation (V-208).

---

## Webhooks (v1.6+)

<!-- TODO: Add screenshot -->

Outbound push subscriptions: register an HTTPS consumer URL and Sentry POSTs each visible `integration_event` to it via the `sentry-dispatcher` daemon. Subscriptions deliver in commit order, sign every request with HMAC-SHA256, retry failures on an exponential schedule (8 attempts, ~15h cumulative, +/-10% jitter per slot since v1.6.1 #234), and dead-letter on the eighth failure.

- **List subscriptions** - URL, status (active / paused / revoked), last-24h success rate, current pending count, current DLQ count
- **Create wizard**:
    - **Connector ID** - dropdown sourced from `/api/admin/connector-registry`
    - **Delivery URL** - HTTPS-validated; HTTP refused unless `SENTRY_ALLOW_HTTP_WEBHOOKS=true` (which itself refuses to boot in production combined with `SENTRY_ALLOW_INTERNAL_WEBHOOKS=true`)
    - **Subscription filter** - strict-typed Pydantic with `extra='forbid'`; `event_types`, `warehouse_ids`, `aggregate_external_id_allowlist` arrays. Empty arrays are refused (#231) -- omit the field for "all values"; use `status='paused'` for "no events".
    - **Rate limit** - per-second cap (1-100 req/s; default 50)
    - **Pending ceiling** - default 10,000; deployment hard cap via `DISPATCHER_MAX_PENDING_HARD_CAP`
    - **DLQ ceiling** - default 1,000; deployment hard cap via `DISPATCHER_MAX_DLQ_HARD_CAP`
    - **One-shot secret reveal modal** - the HMAC shared secret displays exactly once on creation; consumers copy it into their secret store. Subsequent reads return only the cipher.
    - **URL-reuse warning modal** - if the URL was tombstoned by a prior hard delete, the create flow refuses 409 `url_reuse_tombstone` until the admin acknowledges via `acknowledge_url_reuse=true`. v1.6.1 #218 canonicalizes the URL before the gate (case / port / fragment / trailing-slash variants all match the same tombstone).
- **Per-row actions**: edit, pause / resume, rotate secret, view DLQ, view stats, soft-revoke, hard-purge.
- **Rotate secret** runs the 24-hour dual-accept rotation. The previous generation (`gen=2`) stays valid for 24 hours so consumers can roll their verifier without downtime; the dispatcher signs with `gen=1`. Plaintext is revealed exactly once.
- **DLQ panel** with replay-one + replay-batch:
    - Replay-one re-INSERTs a fresh `pending` row pointing at the original event_id.
    - Replay-batch supports filter (status, event_type, warehouse_id, completed_at window) with a server-computed impact estimate (`matched_with_event_data` replayable + `matched_without_event_data` pruned). 10,000-row hard cap requires `acknowledge_large_replay=true`.
    - Throttles: 60-second per-subscription bucket and the v1.6.1 #224 aggregate cross-subscription throttle (default 5 batches per 5 minutes across the deployment).
- **Stats panel** - 6-counter rollups (attempts / succeeded / failed / dlq / in_flight / pending) plus p50 / p95 / p99 response_time_ms, top 5 error_kinds, current cursor lag. Window options: 1h, 6h, 24h, 7d. 30-second in-process cache.
- **Cross-subscription error log** at sidebar -> System -> Webhook errors. Every delivery failure with the server-owned categorical description (`timeout` / `connection` / `tls` / `4xx` / `5xx` / `ssrf_rejected` / `unknown`) and triage hint. v1.6.0 #204 replaced consumer-response-body capture with this server-controlled catalog so the DLQ viewer is not a credential-exfiltration channel.
- **audit_log** writes on every CRUD / rotate / replay (`WEBHOOK_*` action types).

See the [webhook DLQ triage runbook](runbooks/webhook-dlq-triage.md), [webhook pending-ceiling runbook](runbooks/webhook-pending-ceiling.md), [webhook secret compromise runbook](runbooks/webhook-secret-compromise.md), and the consumer integration guide at [`docs/api/webhooks.md`](api/webhooks.md).

---

## Inbound Activity (v1.7+)

<!-- TODO: Add screenshot -->

Read-only observability for v1.7.0 inbound POSTs. Lists recent inbound rows joined to the issuing token, source_system, and canonical resource. The page does not mutate inbound state -- inbound is upsert-or-409 from the API; admin operators investigate here, retry from the source system if needed.

- **List rows** - source_system, resource (sales_orders / items / customers / vendors / purchase_orders), external_id, external_version, status (`accepted` / `stale_version` / `lookup_miss`), received_at, ingested_via_token_id
- **Filters**:
    - Source system (dropdown sourced from `inbound_source_systems_allowlist`)
    - Resource (dropdown of the five canonical resources)
    - Status (`accepted` / `stale_version` / `lookup_miss`)
    - Time range
- **Per-row drilldown** shows the staged `source_payload` JSON, the resolved `canonical_payload`, and the audit_log entry from the upsert with the active mapping doc's sha256 (so investigators can correlate which mapping doc was active when this row was processed).
- **`source_payload` retention** - rows older than `SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS` (default 90 days; 7-day hard floor enforced at boot) have `source_payload` NULLed out by the retention beat task. The row stays in `inbound_*` for forensic recall (external_id + external_version + canonical_payload preserved); only the raw source-system payload falls off.

The cross-row health view at the top of the page shows per-source counts of `accepted` / `stale_version` / `lookup_miss` over the active filter window, which is the fastest path to "is this source system healthy right now?"

See [Deployment -- Inbound (v1.7.0)](deployment.md) for the operator setup (allowlist row, mapping doc, token issuance, restart).
