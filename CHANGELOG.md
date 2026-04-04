# Changelog

All notable changes to Sentry WMS will be documented in this file.

## [v0.8.1] - 2026-04-04

### Added
- Wave picking workflow for combining identical items across multiple sales orders
- `POST /api/picking/wave-validate` - lightweight SO barcode validation before adding to wave
- `POST /api/picking/wave-create` - creates wave batch with combined picks and optimized walk path
- `wave_pick_orders` table linking SOs to wave batches
- `wave_pick_breakdown` table tracking per-SO contributions to combined pick tasks
- Contributing orders shown on `GET /api/picking/batch/<id>/next` with pick_number/total_picks
- Short pick FIFO distribution across contributing orders (fills earlier SOs first)
- Confirm pick updates all contributing SO lines via wave breakdown records
- ERP connector stub (`connector_stub.py`) with `enrich_order()` placeholder for future integration
- 19 wave picking tests covering validation, creation, breakdown, short distribution, and full flow

## [v0.8.0] - 2026-04-04

### Added
- React admin panel frontend (`admin/` directory) built with Vite + React Router
- Login page with JWT authentication and token persistence
- Dashboard with pipeline bar (To Receive, Put-away, To Pick, To Pack, To Ship, Low Stock)
- Dashboard order table, low stock alerts, recent activity feed, and inbound PO table
- Inventory overview page with search and pagination
- Cycle count page with bin selection and count creation
- Receiving page with PO list and line detail modal
- Put-away page showing items in staging bins
- Picking page with orders ready to pick
- Packing page with orders waiting to pack
- Shipping page with orders waiting to ship
- Bin management page with create, detail view, edit, and inventory contents
- Zone management page with create and edit
- Item management page with search, create, detail view, edit, and soft delete
- User management page with create, edit, role assignment, and deactivation
- Audit log viewer with action type, user, and date range filters
- Settings page with warehouse config, CSV/JSON import, manual PO/SO creation, and version info
- Reusable components: DataTable (with CSV export), StatusTag, Pipeline, Modal, PageHeader
- Sidebar navigation organized by warehouse workflow (Floor, Inbound, Outbound, Warehouse, System)
- Sidebar count badges from dashboard stats
- API client with JWT auto-injection and 401 redirect
- CSS custom properties for theming with Instrument Sans and JetBrains Mono fonts
- Docker support for admin panel in docker-compose.yml
- Vite dev server with API proxy to Flask backend

## [v0.7.0] - 2026-04-04

### Added
- Full admin CRUD API for the web admin panel (`/api/admin` blueprint)
- Warehouse management: list, get (with zones), create, update
- Zone management: list (filter by warehouse), create with type validation, update
- Bin management: list (filter by warehouse/zone), get (with inventory), create with type validation, update
- Item management: list with pagination and category/active filters, get (with inventory locations), create with SKU/UPC uniqueness, update, soft delete (blocks if inventory exists)
- Purchase order management: list with pagination and status/warehouse filters, get (with lines), create with lines, update (OPEN only), close
- Sales order management: list with pagination and status/warehouse filters, get (with lines), create with lines, update (OPEN only), cancel (releases allocated inventory if ALLOCATED)
- User management: list (excludes password_hash), create with bcrypt hashing and role validation, update (including password change), soft delete (blocks self-deactivation)
- Audit log viewer: paginated list with action_type, user_id, date range filters
- Inventory overview: paginated list with warehouse/item filters, joins item and bin details
- CSV/JSON bulk import for items and bins with per-row validation and error reporting
- Dashboard stats endpoint: open POs, pending receipts, putaway queue, order pipeline counts, total SKUs/bins, low stock alerts, recent activity feed
- Role enforcement: write operations require ADMIN or MANAGER role, read operations open to all authenticated users

## [v0.6.0] - 2026-04-02

### Added
- Cycle counting workflow: create counts, view expected vs actual, submit with variance detection
- `POST /api/inventory/cycle-count/create` - create cycle counts for one or more bins with inventory snapshot
- `GET /api/inventory/cycle-count/<count_id>` - view count with expected quantities and count status
- `POST /api/inventory/cycle-count/submit` - submit physical counts, auto-create adjustments for variances
- Inventory adjustment records with reason codes and cycle count linkage
- General-purpose bin transfers for stock reorganization
- `POST /api/transfers/move` - move items between any two bins with audit trail
- Automatic inventory correction on cycle count variance (updates quantity_on_hand)
- Last-counted-at tracking on inventory rows

## [v0.5.0] - 2026-04-02

### Added
- Packing workflow: scan-to-verify pack station with barcode validation
- `GET /api/packing/order/<barcode>` - load order for packing with calculated weight
- `POST /api/packing/verify` - scan item barcode to verify against picked list
- `POST /api/packing/complete` - mark order fully packed after all items verified
- Shipping / fulfillment workflow: record tracking info and create fulfillment records
- `POST /api/shipping/fulfill` - submit shipment with tracking number, carrier, and ship method
- Fulfillment line traceability (links shipped items back to source pick bins)
- Calculated package weight from item weights × picked quantities
- Over-pack prevention (blocks verifying more than picked quantity)
- Status enforcement: packing requires PICKING status, shipping requires PACKED status

## [v0.4.0] - 2026-04-02

### Added
- Batch picking with pick path optimization (`pick_sequence`-based serpentine walk)
- `POST /api/picking/create-batch` - create pick batch from multiple SOs with inventory allocation
- `GET /api/picking/batch/<id>` - full batch with tasks in walk-path order
- `GET /api/picking/batch/<id>/next` - next pending pick task
- `POST /api/picking/confirm` - confirm pick with barcode validation (rejects wrong scans)
- `POST /api/picking/short` - report short picks with shortage tracking
- `POST /api/picking/complete-batch` - complete batch, update SO statuses
- Picking service (`picking_service.py`) with core allocation and path optimization logic

## [v0.3.0] - 2026-04-02

### Added
- Receiving workflow: scan PO barcode, verify items, submit receipt to staging bin
- Put-away workflow: pending items list, bin suggestion (default bin or stock consolidation), scan-to-confirm transfer
- `GET /api/receiving/po/<barcode>` - PO lookup with lines and expected items
- `POST /api/receiving/receive` - submit item receipts with inventory updates
- `GET /api/putaway/pending/<warehouse_id>` - items in staging awaiting put-away
- `GET /api/putaway/suggest/<item_id>` - suggested bin for put-away
- `POST /api/putaway/confirm` - confirm put-away with bin transfer record
- Reusable audit logging service (`audit_service.py`)
- Over-receipt warnings (allowed but flagged)

## [v0.2.0] - 2026-04-02

### Added
- JWT authentication system (`POST /api/auth/login`, `POST /api/auth/refresh`)
- `@require_auth` and `@require_role` middleware decorators
- Item lookup by barcode (`GET /api/lookup/item/<barcode>`) with inventory locations
- Bin lookup by barcode (`GET /api/lookup/bin/<barcode>`) with contents
- Item search (`GET /api/lookup/item/search?q=`) - case-insensitive by SKU, name, UPC
- Bin search (`GET /api/lookup/bin/search?q=`) - case-insensitive by code
- User model with bcrypt password verification
- Auth service with JWT token generation and validation
- Password hashing utility (`scripts/hash_password.py`)

## [v0.1.0] - 2026-04-02

### Added
- Initial project structure matching build plan
- PostgreSQL schema - 20 tables covering warehouses, zones, bins, items, inventory, POs, SOs, pick batches, fulfillments, audit log, users
- Flask API skeleton with `/api/health` endpoint
- Docker Compose for local development (PostgreSQL 16 + Flask API)
- Apartment test lab seed data (1 warehouse, 5 zones, 9 bins, 10 items, sample PO + SOs)
- README, CONTRIBUTING.md, LICENSE (MIT), .gitignore, .env.example
