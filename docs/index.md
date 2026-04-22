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

v1.5.0 -- Outbound Poll release. External systems can now consume every inventory-changing write Sentry performs via a cursor-paginated REST read. Introduces a transactional outbox (`integration_events`) populated by seven emission sites in the same DB transaction as the state change that caused it, a deferred-constraint `visible_at` trigger that keeps the poll in commit order even when BIGSERIAL allocates `event_id` out of commit order, a bulk-snapshot endpoint for the initial load backed by a `pg_export_snapshot` keeper daemon, X-WMS-Token inbound auth with hash-only `wms_tokens` storage (SHA-256 + pepper), and admin-panel CRUD for both the connector registry and consumer groups. Five migrations (020-024) plus 025 to drop the `external_id` DEFAULT. New `SENTRY_TOKEN_PEPPER` env var is required; new `snapshot-keeper` Compose service runs alongside `api`. 903 backend tests passing. See the [changelog](changelog.md), [SECURITY.md](https://github.com/hightower-systems/sentry-wms/blob/main/SECURITY.md), and the [v1.5.0 release](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.5.0).

Licensed under MIT. Built by [Hightower Systems](https://github.com/hightower-systems).
