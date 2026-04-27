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

v1.5.1 -- Security audit patch. Closes ~22 findings from the post-v1.5.0 internal audit of the Outbound Poll attack surface (X-WMS-Token vault, `/api/v1/events*`, `/api/v1/snapshot/*`, `integration_events`, snapshot-keeper daemon, admin token / consumer-group / connector-registry CRUD). No new features, no API contract changes; what changed is enforcement strictness. See the [v1.5.1 release](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.5.1) for the full operator-action and findings list. The underlying feature set lands in [v1.5.0](https://github.com/hightower-systems/sentry-wms/releases/tag/v1.5.0) (Outbound Poll: transactional outbox, `visible_at` commit-ordering trigger, bulk-snapshot endpoint, hash-only token vault, admin CRUD). 910 backend tests passing. See the [changelog](changelog.md) and [SECURITY.md](https://github.com/hightower-systems/sentry-wms/blob/main/SECURITY.md).

Licensed under MIT. Built by [Hightower Systems](https://github.com/hightower-systems).
