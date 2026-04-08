# Changelog

All notable changes to Sentry WMS will be documented in this file.

## [v0.9.2] - 2026-04-08

### Fixed
- Test suite refactored from per-test TRUNCATE+reseed to transaction rollback (261 tests in ~4.3s, fixes 365-min CI deadlock)
- ScanInput auto-refocus every 500ms and auto-submit after 100ms pause for C6000 hardware scanner
- Expanded ignored keys in ScanInput (F1-F12, Tab, Escape, GoBack)

### Added
- Short pick admin reporting endpoint (GET /api/admin/short-picks) with SKU, bin, expected/picked/shortage, picker, timestamp
- Short pick count on dashboard pipeline (7d rolling, red when > 0)
- Pick walk item detail modal — tap any item card for SKU, UPC, bin, zone, qty, contributing orders
- `count_show_expected` setting enforced (hides expected qty for blind counts)

### Changed
- Bin types simplified from 6 (RECEIVING, PICKING, BULK, STAGING, SHIPPING, QC) to 3 (Staging, PickableStaging, Pickable)
- Migration: db/migrations/011_bin_type_qc_used.sql
- Updated across: schema.sql, seed data, admin.py, picking_service.py, putaway.py, PutAwayScreen.js, Settings.jsx, Bins.jsx
- Seed data fully rewritten to match 61 printed Zebra barcode labels (20 items, 16 bins, 5 POs, 20 SOs)
- All 12 test files rewritten for new seed data
- 49 files changed, +2,089 / -665 lines

## [v0.9.1] - 2026-04-06

### Fixed
- Put-Away missing from home screen (allowed_functions didn't include 'putaway')
- Receiving confirm fails with PO_id error
- Cycle count "Failed to create count" (FK constraint on inventory_adjustments)
- ScanInput doesn't clear after scan
- Double-tap required on home screen buttons
- One scan confirms entire pick quantity (now one scan = one unit)
- Pick quantities showing zeros (field mapping for line_count/total_units)
- End-of-batch flow redesign with Submit/Cancel
- Admin login shows no error on wrong password
- SO status lifecycle (removed ALLOCATED, added proper PICKING/PICKED statuses)

### Added
- Two receiving modes: Standard (manual qty entry) and Turbo (each scan = 1 unit)
- User icon dropdown menu with Logout
- Second warehouse for testing
- Preferred bins system with put-away suggestions (`preferred_bins` table)
- SKU display on pick walk screen
- Admin preferred bins page with full CRUD, inline priority editing, CSV export
- Admin cycle counts page with detail modal (expected/counted/variance breakdown)
- `count_show_expected` app setting for hiding expected quantities during counts
- `useScanQueue` hook for sequential barcode processing in turbo mode
- `POST /api/putaway/update-preferred` - set/change preferred bin from mobile
- `GET/POST/PUT/DELETE /api/admin/preferred-bins` - admin CRUD for preferred bins
- `GET /api/admin/cycle-counts` - cycle count list with line details
- `GET/PUT /api/admin/settings` - app settings management

### Changed
- Put-away flow redesigned: scan item → see preferred bin suggestion → scan destination → optional preferred bin prompt
- Receiving screen restructured to match pick scan pattern (PO queue → work through items)
- Count screen supports Standard/Turbo modes with AsyncStorage persistence
- Suggest bin endpoint queries `preferred_bins` table first, falls back to `default_bin_id`
- Items admin page shows default bin column from preferred bins

### Database
- New `preferred_bins` table with priority ranking and UNIQUE(item_id, bin_id)
- Seed data reset to match printed Zebra labels (fly fishing catalog)
- PO quantities reduced to 5–10, SO quantities reduced to 1–2 for lab testing

## [v0.9.0] - 2026-04-04

### Added
- React Native / Expo mobile scanner app (`mobile/` directory) for warehouse floor operations
- 10 screens: Login, Home, Receive, Put-Away, Pick Scan (wave), Pick Walk, Pick Complete, Pack/Ship, Cycle Count, Transfer
- 5 shared components: ScanInput (keyboard wedge), ErrorPopup (blocking modal), ActiveBatchBanner, WarehouseSelector, PagedList
- Hardware barcode scanner support via keyboard wedge (TextInput capture on Enter key)
- JWT auth context with session timeout (8-hour default), auto-logout on app foreground
- API client (native fetch) with JWT interceptor and 401 auto-logout
- Stack navigation (React Navigation) with auth-gated routing
- Universal scan bar on home screen (item/bin lookup from any barcode)
- Role-based function visibility on home screen (ADMIN sees all, others see allowed_functions)
- Active batch resume banner on home screen
- Warehouse switching from header tap
- Wave picking: scan SOs, build batch, walk pick path with zone/aisle display
- Short pick modal with quantity input
- Contributing orders collapsible section on pick walk
- Pack verification: scan-to-verify each item, then ship with carrier/tracking
- Cycle count: scan bin, enter counts, auto-variance detection
- Transfer: 3-step scan flow (item, from bin, to bin) with quantity input
- Brand theme: Accent Red (#8e2715), Copper (#c4722a), Cream (#FCF4E3), monospace typography, 48dp tap targets
- `GET /api/picking/active-batch` - returns user's incomplete pick batch for resume
- `GET /api/warehouses/list` - public endpoint (no auth) for login screen warehouse selector
- `GET /api/auth/me` - returns user info with role-based allowed_functions
- `app_settings` table for configurable session timeout
- `allowed_functions` column on users table for per-user function visibility
- Migration: `db/migrations/009_mobile_app.sql`
- 9 new API tests in `test_mobile_endpoints.py` (warehouse list, auth/me, active batch, session settings)

## [v0.8.2] - 2026-04-04

### Changed
- `GET /api/picking/batch/<id>/next` now includes explicit `zone` and `aisle` fields
- Zone and aisle return as null (not empty string) when bin has no zone or aisle assignment
- Pick task queries use LEFT JOIN on zones for bins without zone assignment
- Added zone_name to batch task list and next-task responses

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
