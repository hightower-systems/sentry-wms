# Sentry WMS

Sentry WMS is a free, open-source warehouse management system built for e-commerce fulfillment.

It connects barcode scans, pick tasks, and inventory movements to whatever database or ERP your business runs on. Sentry handles the physical warehouse execution layer -- receiving, storage, picking, packing, shipping, and counting -- so your system of record stays accurate.

## Features

- **Receiving** -- scan PO barcodes, verify items, stage for put-away
- **Put-Away** -- suggested bin placement with preferred bin priorities, scan-to-confirm storage
- **Pick Walk** -- multi-order batch picking with serpentine walk path optimization
- **Pack Verification** -- scan-to-verify pack station with item-by-item confirmation
- **Shipping** -- carrier and tracking entry, fulfillment recording
- **Cycle Counting** -- bin-level counts with variance detection and admin approval workflow
- **Bin-to-Bin Transfer** -- move inventory between locations with audit trail
- **Inter-Warehouse Transfer** -- cross-warehouse inventory moves
- **Inventory Adjustments** -- direct add/remove with reason tracking
- **Barcode Lookup** -- scan any barcode from the home screen to identify items, bins, POs, or SOs
- **Connector Framework** -- pluggable ERP / commerce sync with encrypted credential vault, sync-health dashboard, rate limiting, and circuit breaker
- **Admin Panel** -- React web app for warehouse managers to monitor operations and configure the system

## Stack

| Layer | Technology |
|-------|-----------|
| Mobile App | React Native (Expo) |
| API | Python / Flask |
| Database | PostgreSQL 16 |
| Admin Panel | React 18 / Vite |
| Infrastructure | Docker Compose |

## Quick Start

```bash
git clone https://github.com/hightower-systems/sentry-wms.git
cd sentry-wms
cp .env.example .env
# Set every required secret inside .env (JWT_SECRET, SENTRY_ENCRYPTION_KEY,
# REDIS_PASSWORD). See the comments in .env.example for generation commands.
docker compose up -d
```

- API: [http://localhost:5000](http://localhost:5000)
- Admin panel: [http://localhost:8080](http://localhost:8080)
- Health check: [http://localhost:5000/api/health](http://localhost:5000/api/health)

Fresh installs seed the admin user as `admin` / `admin` with a forced password change on first login. Set `ADMIN_PASSWORD` in your `.env` to skip the forced-change flow; the seed prints that value in the logs:

```bash
docker compose logs db | grep "Admin password"
```

For local development with Vite dev-server and hot reload, layer on the
dev overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

## Documentation

- [API Reference](api-reference.md) -- every endpoint with request/response examples
- [Deployment](deployment.md) -- Docker setup, production config, mobile app
- [Admin Panel](admin-panel.md) -- page-by-page guide to the web admin
- [Test Lab](test-lab.md) -- setting up a test environment with hardware scanners
- [Contributing](contributing.md) -- how to set up the dev environment and submit PRs

## Current Version

v1.10.0 -- POS endpoint surface. Sentry serves a dedicated counter-sale API for an external POS Service: four endpoints under `/api/v1/pos/` (`GET /availability`, `POST /validate-cart`, `POST /checkout`, `POST /refund`) authenticated by a new fourth direction `pos.dispatch` alongside outbound polling, inbound POST, and dockd. Checkout and refund are atomic single-transaction routes with `SELECT ... FOR UPDATE` on the inventory rows being decremented or re-incremented, idempotent on a per-route `idempotency_key` (UUID4) with a SHA-256 body hash so a retry with the same key + same body replays the cached response and a retry with the same key + different body returns 409. Refund enforces a 90-day window from the original sale's `created_at`, a card-vs-cash tender lock, and a once-per-original-SO guard via `refunded_at` / `refund_so_id` on the original `sales_orders` row. PCI-scope guard at the Pydantic boundary: card tenders accept exactly `{type, amount_cents, card_brand, card_last4, auth_code, external_ref}`; any other field fails 422. Pricing stays out of Sentry: per-line cents ride on the wire and land in `audit_log.details`; the POS Service owns its own pricing source. New `ACTION_POS_CHECKOUT` + `ACTION_POS_REFUND` audit constants. One migration (056). No new APK; v1.9.0 APK remains the working baseline since v1.10 adds no mobile changes. See the [v1.10.0 release](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.10.0).

v1.9.0 -- Dockd shipping integration. Sentry serves a dedicated outbound shipping API for the in-warehouse dockd application: three endpoints under `/api/v1/dockd/orders/<so_number>` (GET, ship, void-ship) authenticated by per-station bearer tokens with the new `dockd.dispatch` scope, idempotent under retry through SHA-256 body-hash sentinel rows, and serialized against concurrent shipment via `SELECT ... FOR UPDATE` on the SO. Both ship and void-ship write through the existing audit-log hash chain and emit on the `integration_events` outbox; the new `ship.voided/1` event reverts a SHIPPED SO back to PICKED or PACKED. The SO lifecycle gains `CANCELLED` status with end-to-end wiring (admin + inbound + dashboard counter); a new `sales_orders.memo` column flows from connector through the picker / packer / shipper screens; the admin Audit Log page is modernized with color-coded action badges, chip-style detail previews, and a Copy JSON button. PICK / TO_LINE_PICKED / PACK / RECEIVE audit details now record both expected and actual counts. Two migrations (054-055). v1.9 APK published (`sentry-wms-v1.9.0.apk`) for the new memo display and the pack-after-short-pick fix; v1.8 APK remains a working baseline. See the [v1.9.0 release](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.9.0).

v1.8.0 -- Transfer Orders + Productivity Dashboard. Sentry's first internal warehouse-to-warehouse workflow: import a TO via CSV (with shortage detection), pick through the existing mobile flow via a new `pick_tasks.to_id` discriminator, batch picks into an admin-approval row, approve to move inventory source -> destination + emit `transfer.completed/1`, or reject for re-pick. The operations-overview Dashboard is replaced with a per-user productivity grid (Picking units / Packing units / Shipped orders / Received unique SKUs / Put Away unique SKUs) backed by `audit_log` aggregation through a new compound covering index. Inbound contract gains `sales_orders.order_total` + `customer_shipping_paid` (NUMERIC(12,2) with per-field decimal bounds), structured 16-column billing + shipping addresses (drops v1.7's two TEXT placeholders), inbound line items write through to `purchase_order_lines` + `sales_order_lines`, per-token static `mapping_overrides` JSONB, and a `warehouse_id` token fallback. Five migrations (049-053). Three security carry-forwards close the v1.4 deferral set. See the [v1.8.0 release](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.8.0).

v1.7.0 -- Inbound (Pipe B). External systems can now POST canonical-shaped resource updates to Sentry through five new endpoints under `/api/v1/inbound/` (sales_orders, items, customers, vendors, purchase_orders). Each request carries `external_id` + `external_version` + `source_payload`; per-source mapping documents (YAML at `db/mappings/<source_system>.yaml`) translate the payload into Sentry's canonical model with strict-typed Pydantic validation, JSONPath resolution, simpleeval-sandboxed derived expressions, and `cross_system_lookup` for canonical UUID resolution against prior ingestions. `X-WMS-Token` gains `source_system` + `inbound_resources` scope dimensions; `inbound_source_systems_allowlist` gates which source systems can POST. Twelve new migrations (037-048). One new env var (`SENTRY_INBOUND_SOURCE_PAYLOAD_RETENTION_DAYS`); two existing env vars gain new shape (`SENTRY_INBOUND_MAX_BODY_KB` boot-validated, `SENTRY_INBOUND_MAPPINGS_DIR` default changed to absolute `/db/mappings`). audit_log chain integrity hardened against concurrent insert via sentinel-lock + nextval-in-trigger (#271). Direct-DB revoke of `wms_tokens.revoked_at` now propagates auth invalidation across workers (#274, #278). License changed from MIT to Apache 2.0; pre-v1.7.0 tagged releases remain MIT-licensed. See the [v1.7.0 release](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.7.0) for migrations, env vars, and operator notes. See the [changelog](changelog.md) and [SECURITY.md](https://github.com/hightower-systems/sentry-wms/blob/main/SECURITY.md).

v1.6.1 -- Webhook Security Patch. Closes 22 findings (V-300 through V-321) from the post-v1.6.0 audit on the new outbound webhook surface: tombstone-gate URL canonicalization, HMAC-signed cross-worker pubsub, secret-rotation race closed via `SELECT FOR SHARE`, replay-batch pre-INSERT ceiling check + cross-subscription throttle, response-body cap + tuple HTTP timeouts with wall-clock watchdog, malformed-filter fail-closed, retry-slot jitter, `webhook_deliveries` DELETE/TRUNCATE forensic triggers, and api-container boot-guard parity with the dispatcher. Three new migrations (034-036). Five new env vars. No API contract changes. See the [v1.6.1 release](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.6.1) for migrations, env vars, and operator notes.

v1.6.0 -- Outbound Push (Pipe A Write). External systems no longer have to long-poll `integration_events`: a new `sentry-dispatcher` daemon POSTs each visible event to admin-registered consumer URLs over HMAC-signed HTTPS with a 24-hour dual-accept rotation window, exponential-backoff retries, a 1,000-row dead-letter lane, and dispatch-time SSRF guard with DNS-rebinding mitigation. Admin panel gains a Webhooks page (CRUD, secret rotation, DLQ viewer with replay-one + replay-batch, per-subscription stats, cross-subscription error log) and a wired global search bar covering items / bins / POs / SOs / customers (#163, carry-forward from v1.4). See the [v1.6.0 release](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.6.0) for migrations, env vars, and operator notes.

Licensed under Apache 2.0. Built by [Hightower Systems](https://github.com/hightower-systems).
