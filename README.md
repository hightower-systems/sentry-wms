<div align="center">
  <img src="docs/assets/sentry-banner.png" alt="Sentry WMS" width="100%">
  
  <p><em>Open-source warehouse management system built for barcode scanners</em></p>

  ![Version](https://img.shields.io/badge/version-1.0.0-8e2716)
  ![Tests](https://img.shields.io/badge/tests-288%20passing-34a853)
  ![License](https://img.shields.io/badge/license-MIT-blue)

  <img src="docs/assets/sentry-preview.png" alt="Sentry WMS Screenshots" width="100%">
</div>

---

# Sentry WMS

**Open-source warehouse management system built for e-commerce.**

Sentry is the link between the warehouse floor and your system of record. It connects barcode scans, pick tasks, and inventory movements to whatever database or ERP your business runs on.

## What Sentry Does

- **Receiving** - Scan PO barcodes, verify items, stage for put-away
- **Put-Away** - Suggested bin placement, scan-to-confirm storage
- **Picking** - Multi-order batch picking with optimized walk paths
- **Packing** - Scan-to-verify pack station (separate screen from shipping)
- **Shipping** - Carrier/tracking entry, fulfillment recording (separate screen from packing)
- **Cycle Counting** - Bin-level counts with variance detection
- **Bin Transfers** - Move inventory between locations
- **Inter-Warehouse Transfers** - Cross-warehouse inventory moves with audit trail
- **Inventory Adjustments** - Direct add/remove with reason tracking

## What Sentry Is Not

Sentry is not an ERP. It does not manage orders, products, or customers. It connects to your existing systems (NetSuite, QuickBooks, SAP, or any ERP with an API) and handles the physical warehouse execution layer.

## Architecture

| Layer | Technology |
|-------|-----------|
| Mobile App | React Native (Expo)  -  shared hooks (`useScreenError`), reusable components (`ScreenHeader`, `ModeSelector`, `ScanInput`) |
| API | Python / Flask  -  `@with_db` middleware, `inventory_service` + `picking_service` service layer, `constants.py` status enums |
| Database | PostgreSQL 16 (dev Docker) · PostgreSQL Cloud (prod) |
| Admin Panel | React Web App  -  dark theme, warehouse context picker, `WarehouseContext` provider |

## Quick Start

```bash
# Clone the repo
git clone https://github.com/hightower-systems/sentry-wms.git
cd sentry-wms

# Copy environment config
cp .env.example .env

# Start PostgreSQL + API + Admin Panel with Docker
docker-compose up -d

# Or start with a clean system (no demo data):
# SKIP_SEED=true docker-compose up -d

# API is now running at http://localhost:5000
# Admin panel is now running at http://localhost:3000
# Health check: http://localhost:5000/api/health
# Admin password is printed in docker logs on first run

# Start the mobile app (separate terminal)
cd mobile
cp .env.example .env    # Set EXPO_PUBLIC_API_URL to your machine's IP
npm install
npx expo start
```

## Admin Panel

The admin panel is a React web app for warehouse managers to monitor operations and configure the system.

- **Dashboard** - pipeline overview, open orders, low stock alerts, recent activity
- **Inventory** - full inventory view with search and pagination
- **Cycle Counts** - create and track bin-level counts
- **Receiving / Put-Away / Picking / Packing / Shipping** - workflow status views
- **Bins / Zones / Items** - warehouse setup with create, edit, and detail views
- **Adjustments** - direct inventory add/remove with reason tracking
- **Inter-Warehouse Transfers** - move inventory between warehouses
- **Users** - user management with role assignment
- **Audit Log** - filterable log viewer with entity name resolution
- **Import** - CSV/JSON bulk import for items, bins, POs, SOs with templates
- **Settings** - warehouse config, manual PO/SO entry, fulfillment workflow toggles
- **Warehouse Picker** - header dropdown to switch warehouse context (all pages filter dynamically)

Built with React 19, Vite, React Router, and plain CSS. Dark theme with copper accents. No component libraries.

## API Endpoints

### Authentication
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/login` | Device login, returns JWT token |
| POST | `/api/auth/refresh` | Refresh an existing token |

### Lookups
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/lookup/item/<barcode>` | Scan item → details + bin locations |
| GET | `/api/lookup/bin/<barcode>` | Scan bin → contents with quantities |
| GET | `/api/lookup/item/search?q=` | Text search items by SKU, name, UPC |
| GET | `/api/lookup/bin/search?q=` | Text search bins by code |

### Receiving
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/receiving/po/<barcode>` | Scan PO → lines with expected items |
| POST | `/api/receiving/receive` | Submit received items to staging bin |
| POST | `/api/receiving/cancel` | Undo receipts by receipt_ids (reverses inventory + PO lines) |

### Put-Away
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/putaway/pending/<warehouse_id>` | Items in staging awaiting put-away |
| GET | `/api/putaway/suggest/<item_id>` | Suggested bin for put-away |
| POST | `/api/putaway/confirm` | Confirm put-away to destination bin |

### Picking
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/picking/wave-validate` | Validate SO barcode for wave picking |
| POST | `/api/picking/wave-create` | Create wave batch with combined picks across SOs |
| POST | `/api/picking/create-batch` | Create pick batch with optimized walk path |
| GET | `/api/picking/batch/<batch_id>` | Full batch with tasks in walk-path order |
| GET | `/api/picking/batch/<batch_id>/next` | Next pending pick task (includes zone/aisle, nullable) |
| POST | `/api/picking/confirm` | Confirm a pick with barcode validation |
| POST | `/api/picking/short` | Report a short pick |
| POST | `/api/picking/complete-batch` | Mark batch complete |

### Packing
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/packing/order/<barcode>` | Scan SO → picked items to verify with weight |
| POST | `/api/packing/verify` | Scan item barcode to verify during packing |
| POST | `/api/packing/complete` | Mark order fully packed |

### Shipping
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/shipping/fulfill` | Submit shipment with tracking + carrier info |

### Cycle Counting
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/inventory/cycle-count/create` | Create cycle counts for bins with inventory snapshot |
| GET | `/api/inventory/cycle-count/<count_id>` | View count with expected vs counted quantities |
| POST | `/api/inventory/cycle-count/submit` | Submit counts, auto-adjust variances |

### Bin Transfers
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/transfers/move` | Move items between bins |

### Inventory Adjustments & Inter-Warehouse Transfers
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/admin/adjustments/direct` | Create and auto-approve inventory adjustment |
| GET | `/api/admin/adjustments/list` | List adjustments with item/bin details |
| POST | `/api/admin/inter-warehouse-transfer` | Move inventory between warehouses |
| GET | `/api/admin/inter-warehouse-transfers` | Recent inter-warehouse transfer history |

### Admin CRUD
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/warehouses` | List warehouses |
| GET | `/api/admin/warehouses/<id>` | Get warehouse with zones |
| POST | `/api/admin/warehouses` | Create warehouse |
| PUT | `/api/admin/warehouses/<id>` | Update warehouse |
| GET | `/api/admin/zones` | List zones (filter by warehouse) |
| POST | `/api/admin/zones` | Create zone |
| PUT | `/api/admin/zones/<id>` | Update zone |
| GET | `/api/admin/bins` | List bins (filter by warehouse/zone) |
| GET | `/api/admin/bins/<id>` | Get bin with inventory |
| POST | `/api/admin/bins` | Create bin |
| PUT | `/api/admin/bins/<id>` | Update bin |
| GET | `/api/admin/items` | List items (paginated, filter by category/active) |
| GET | `/api/admin/items/<id>` | Get item with inventory locations |
| POST | `/api/admin/items` | Create item |
| PUT | `/api/admin/items/<id>` | Update item |
| DELETE | `/api/admin/items/<id>` | Deactivate item (soft delete) |
| GET | `/api/admin/purchase-orders` | List POs (paginated, filter by status) |
| GET | `/api/admin/purchase-orders/<id>` | Get PO with lines |
| POST | `/api/admin/purchase-orders` | Create PO with lines |
| PUT | `/api/admin/purchase-orders/<id>` | Update PO (OPEN only) |
| POST | `/api/admin/purchase-orders/<id>/close` | Close PO |
| GET | `/api/admin/sales-orders` | List SOs (paginated, filter by status) |
| GET | `/api/admin/sales-orders/<id>` | Get SO with lines |
| POST | `/api/admin/sales-orders` | Create SO with lines |
| PUT | `/api/admin/sales-orders/<id>` | Update SO (OPEN only) |
| POST | `/api/admin/sales-orders/<id>/cancel` | Cancel SO (releases inventory) |
| GET | `/api/admin/users` | List users |
| POST | `/api/admin/users` | Create user |
| PUT | `/api/admin/users/<id>` | Update user |
| DELETE | `/api/admin/users/<id>` | Delete user (hard delete) |
| GET | `/api/admin/audit-log` | Audit log (paginated, filterable) |
| GET | `/api/admin/inventory` | Inventory overview (paginated) |
| POST | `/api/admin/import/<type>` | Bulk import items or bins |
| GET | `/api/admin/dashboard` | Dashboard stats and counts |
| GET | `/api/admin/short-picks` | Short pick report (filter by days, warehouse) |

## Database

### Bin Types

Sentry uses 3 bin types that control whether the pick algorithm can pull inventory:

| Type | Pickable? | Purpose |
|------|-----------|---------|
| `Staging` | No | Inbound dock, QC hold. Inventory lands here on receipt. Put-away moves it out. |
| `PickableStaging` | Yes | Staging area where admin allows pickers to pull fresh inventory before formal put-away. |
| `Pickable` | Yes | Standard shelf bins, bulk storage, shipping desk. Default for most bins. |

### Test Lab Seed Data

The apartment lab seed (`db/seed-apartment-lab.sql`) matches 61 printed Zebra barcode labels:

- 2 warehouses, 6 zones, 16 bins
- 20 items (fly fishing catalog, TST-001 through TST-020)
- 5 purchase orders (10/3/8/5/1 lines)
- 20 sales orders (single-item, multi-item, contention, serpentine walk, short pick test)

Set `SKIP_SEED=true` to start with a clean system (admin user + one empty warehouse only, no demo data).

### Security

- JWT authentication with live database validation on every request - `require_auth` verifies the user's role, warehouse access, and active status per-request (not cached in the token)
- Deactivated users and permission changes take effect immediately
- Required `JWT_SECRET` environment variable (crashes on startup if missing)
- Warehouse authorization middleware - non-admin users blocked from unassigned warehouses (403)
- Lookup endpoints enforce warehouse isolation - users only see inventory, bins, and orders for their assigned warehouses
- Login lockout - 5 failed attempts locks the account for 15 minutes
- All SQL queries use parameterized bindings (no string interpolation of user input)
- bcrypt password hashing with salt, minimum 8-character password policy
- Random admin password generated at seed time (no default credentials)
- Over-pick and negative quantity prevention on all warehouse operations
- Security response headers (X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy)
- Stack trace suppression in production (generic 500 error responses)
- CORS restricted to explicit origin whitelist
- Role-based access control (ADMIN/USER) with function-level visibility
- Non-root container with gunicorn (4 workers) in production
- Full audit trail on every warehouse action

### Testing

288 tests using transaction rollback isolation (savepoint per test, rollback after). Runs in ~5 seconds.

```bash
docker compose exec api python -m pytest tests/ -v --tb=short
```

## Project Status

**v1.0.0 - Production Release**

| Version | Milestone | Status |
|---------|-----------|--------|
| v0.1.0 | Foundation - project structure, schema, Docker | ✅ Complete |
| v0.2.0 | JWT auth, item/bin lookups | ✅ Complete |
| v0.3.0 | Receiving + put-away | ✅ Complete |
| v0.4.0 | Batch picking with path optimization | ✅ Complete |
| v0.5.0 | Pack + ship (separate screens) | ✅ Complete |
| v0.6.0 | Inventory management (cycle counts, transfers) | ✅ Complete |
| v0.7.0 | Admin CRUD API | ✅ Complete |
| v0.8.0 | React admin panel | ✅ Complete |
| v0.8.1 | Wave picking with combined SO batches | ✅ Complete |
| v0.9.0 | Mobile scanner app (12 screens, C6000 support) | ✅ Complete |
| v0.9.1 | Apartment lab testing, preferred bins, bug fixes | ✅ Complete |
| v0.9.2 | Test infrastructure, bin type simplification, short pick reporting | ✅ Complete |
| v0.9.3 | UI revamp - tan cards, accent stripes, carrier picker, blind counts | ✅ Complete |
| v0.9.4 | Structural refactor - service layer, admin split, shared styles/hooks | ✅ Complete |
| v0.9.5 | Scan hardening, cycle count approval, admin UX overhaul, CSV templates | ✅ Complete |
| v0.9.6 | Scan hardening, put-away reorder, manual picking, role simplification | ✅ Complete |
| v0.9.7 | 27-bug hardware test fix (repeat offenders, styled modals, EAS build) | ✅ Complete |
| v0.9.8 | Admin dark theme, warehouse picker, security hardening, status constants, SKIP_SEED | ✅ Complete |
| v0.9.9 | SQL parameterization, warehouse auth, JWT hardening, FK indexes, scanner plugin fix | ✅ Complete |
| **v1.0.0** | **Production release - full security audit, penetration test fixes, hardened infrastructure** | ✅ **Released** |
| v2.0.0 | ERP + commerce integration (NetSuite, QuickBooks, Shopify, Fabric, REST API connectors) | Planned |

See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT - see [LICENSE](LICENSE) for details.

Built by [Hightower Systems L.L.C.](https://github.com/hightower-systems) · v1.0.0
