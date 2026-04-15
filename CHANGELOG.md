# Changelog

All notable changes to Sentry WMS will be documented in this file.

## [v1.1.0] - 2026-04-14

### Security - Backlog Audit (12 fixes)
- **Token invalidation on password change (M1)** - added `password_changed_at` column to users table; auth middleware rejects tokens issued before the last password change
- **JWT iat/jti claims (L10)** - tokens now include `iat` (issued-at, unix seconds) and `jti` (UUID) for revocation and replay detection
- **DB-backed rate limiting (M8)** - replaced in-memory `_login_attempts` dict with `login_attempts` table; persistent across restarts, per-username and per-IP tracking (5 attempts, 15 min lockout)
- **Password complexity (L1)** - `validate_password()` enforces minimum 8 characters, at least one letter, at least one digit; applied on user creation, admin password update, and self-service password change
- **Self-service password change (L2)** - `POST /api/auth/change-password` endpoint; mobile UI added as modal in user dropdown (current password, new password, confirm)
- **Warehouse listing auth (L7)** - `GET /api/warehouses/list` now requires JWT; mobile warehouse selection moved from pre-login to a blocking post-login modal on HomeScreen
- **suggest_bin warehouse scope (L8)** - preferred bin and default bin queries filtered to user's allowed warehouses; admins bypass the filter
- **CSV import limit (M10)** - import endpoint rejects payloads with more than 5000 records
- **Cycle count self-approval check (M3)** - configurable `require_count_approval_separation` app setting; when enabled, the counter cannot approve their own cycle count adjustments (403); when disabled, self-approvals are logged as `SELF_APPROVED_COUNT` in the audit log
- **Pagination (M6)** - added `page`/`per_page` query params with `LIMIT`/`OFFSET` to warehouses, zones, bins, and users list endpoints (default 50, max 1000)
- **Cleartext HTTP disabled for production (L5)** - `usesCleartextTraffic` set to false in app.json; `with-cleartext-traffic` plugin now checks `EAS_BUILD_PROFILE` and only enables cleartext for non-production builds
- **Production docker-compose (L6)** - `docker-compose.prod.yml` omits source volume mounts and requires all credentials via env vars

### Admin Panel
- New "Inventory" settings section with "Require separate approver for cycle count adjustments" checkbox
- Version updated to 1.1.0

### Mobile
- Warehouse selection moved from login screen to post-login blocking modal
- "Change Password" option added to user dropdown on home screen
- Auto-selects warehouse if only one is available
- Version updated to 1.1.0

### Infrastructure
- Migration 014: `password_changed_at TIMESTAMPTZ` column on users table
- Migration 015: `login_attempts` table with key, attempts, locked_until, last_attempt columns

### Bug Fixes
- Change-password endpoint returns 403 (not 401) for wrong current password, preventing the mobile client's auto-logout interceptor from firing

### Tests
- 19 new tests (307 total, 0 regressions)
- Warehouse list auth test (401 without JWT, 200 with JWT)
- CSV import limit test (5001 records returns 400)
- Cycle count self-approval tests (both modes)
- Password complexity tests (short, no digit, no letter)
- Self-service password change tests (success, wrong current, weak new, requires auth)
- JWT iat/jti claim tests (presence, uniqueness)
- Token invalidation tests (old token rejected, new token works after password change)
- Per-IP lockout test
- Pagination tests (zones, bins)

## [v1.0.0] - 2026-04-14

### Security - Full Code Audit
- **Default admin password eliminated** - seed script generates random 16-char password at runtime via `/dev/urandom`, prints to docker logs. Set `ADMIN_PASSWORD` env var to override.
- **Password minimum 8 characters** - enforced on user creation and password updates via admin panel
- **Over-pick prevention** - `quantity_picked` capped at `quantity_to_pick` in pick confirmation; prevents inventory drain via API manipulation
- **Inventory floor protection** - picking decrements use `GREATEST(0, ...)` to prevent negative inventory from race conditions
- **Short pick quantity cap** - `quantity_available` validated against task requirement
- **Packing quantity validation** - verify endpoint rejects zero and negative quantities
- **PO/SO line quantity validation** - `quantity_ordered` must be greater than zero on order creation
- **Receiving bin-warehouse validation** - bin must belong to PO's warehouse; prevents cross-warehouse inventory corruption
- **Lookup endpoint warehouse isolation** - item locations, bin contents, SO details, and bin search filtered by user's assigned warehouses (IDOR fix)
- **Security response headers** - X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy on all responses
- **Stack trace suppression** - global 500 error handler returns generic error instead of leaking internals
- **Debug mode disabled** - `debug=False` hardcoded; Werkzeug interactive debugger no longer activatable via env var
- **Login lockout** - 5 failed attempts locks the account for 15 minutes (per-username tracking, resets on successful login)
- **PostgreSQL port bound to localhost** - `127.0.0.1:5432:5432` prevents network exposure

### Infrastructure
- **Gunicorn in production** - Dockerfile CMD switched from `python app.py` (single-threaded Flask dev server) to `gunicorn -w 4` (4 workers)
- **Non-root container** - Dockerfile creates and runs as `appuser` instead of root

### Tests
- 5 new login lockout tests
- Test passwords updated to meet 8-character minimum
- 288 tests passing, 0 regressions

### Version
- All version numbers bumped to 1.0.0 across API, admin, mobile, README

## [v0.9.9] - 2026-04-13

### Security
- **SQL parameterization** - 30+ SQL queries converted from f-string constant interpolation to parameterized bindings across all route and service files (admin_orders, admin_users, packing, picking, receiving, shipping, picking_service)
- **Warehouse authorization middleware** - non-admin users blocked at the request level from accessing unassigned warehouses (checks both query params and JSON body, returns 403)
- **JWT_SECRET required** - app raises `RuntimeError` on startup if missing; `docker-compose.yml` uses `${JWT_SECRET:?}` syntax to error if not set
- **JWT payload includes warehouse_ids** - enables middleware enforcement without DB lookup
- **DB credentials configurable** - PostgreSQL user/password/database use env vars with defaults instead of hardcoded values
- **Debug mode conditional** - Flask debug mode tied to `FLASK_ENV` instead of always-on
- CORS origins now include port 5000 and are logged on startup

### Performance
- **17 FK indexes** added to `schema.sql` - PostgreSQL does not auto-index foreign key columns; these improve JOIN performance and cascading delete efficiency across zones, orders, PO/SO lines, pick tasks, fulfillments, transfers, cycle counts, and audit log

### Mobile
- **First-run setup screen** - detects if no server URL has been saved and shows a dedicated connect screen with health check validation before accepting the URL
- **Chainway scanner plugin fix** - config plugin now detects Kotlin vs Java `MainApplication` and uses correct patterns for package registration (fixes "Native module not available" on standalone APK)
- **Cleartext traffic plugin** - new `with-cleartext-traffic.js` Expo config plugin for Android 9+ HTTP support
- **API client improvements** - `hasStoredApiUrl()` helper, full URL in debug logs, server URL modal validates connectivity before saving

### Admin
- **Auth reload loop fixed** - 401 handler now clears both token and user from localStorage
- **Warehouse fetch gated behind auth** - `WarehouseProvider` waits for authenticated user before fetching warehouses
- **Vitest config** added for frontend testing

### Config
- `.env.example` expanded with all variables, organized with comments, proper `JWT_SECRET` generation instructions

### Tests
- 6 new warehouse authorization tests
- 283 tests passing (was 277)

## [v0.9.8] - 2026-04-11

### Security
- **JWT_SECRET required**  -  app raises `RuntimeError` on startup if `JWT_SECRET` env var is missing (was silently falling back to hardcoded default)
- **CORS restricted**  -  `CORS(app)` wildcard replaced with explicit origin whitelist (`CORS_ORIGINS` env var, defaults to `localhost:3000,localhost:8081`)
- **Explicit allowed-field sets**  -  all admin update endpoints (items, POs, SOs, users, warehouses, zones, bins) now use `ALLOWED_FIELDS` sets instead of iterating arbitrary request keys
- **Dead code removed**  -  unused `_paginate()` helper deleted from admin `__init__.py`

### Admin Panel
- Dark theme overhaul  -  header (#2a2520), sidebar, copper accents, cream text, 48px header with 192px sidebar
- Warehouse picker dropdown in header  -  admin users switch warehouse context from the topbar
- `WarehouseContext` provider persists selection in sessionStorage, auto-selects first warehouse on login
- All pages use dynamic `warehouseId` from context instead of hardcoded `warehouse_id=1`
- All pages re-fetch data automatically when warehouse selection changes
- **Adjustments page**  -  direct inventory add/remove with searchable bin/item pickers, recent adjustments table
- **Inter-Warehouse Transfers page**  -  cross-warehouse inventory moves with cascading warehouse/bin/item selects, transfer history
- **Imports page**  -  merged import type selector and file upload into single card with download template buttons
- Settings: removed import tools (moved to Imports page), added address fields to SO modal, vendor address to PO modal
- Audit log: batch-resolves entity IDs to human-readable names (bins, items, SOs, POs)
- Sidebar: added Adjustments and Transfers nav items under Warehouse group
- 4 new API endpoints: `POST /admin/adjustments/direct`, `GET /admin/adjustments/list`, `POST /admin/inter-warehouse-transfer`, `GET /admin/inter-warehouse-transfers`

### Mobile
- PutAwayScreen: compressed spacing for suggest/item/confirm cards
- TransferScreen: tightened step dots, labels, info cards, quantity row
- PickWalkScreen: reduced bin/item/next card padding and margins
- CountScreen: reduced bin header, turbo card, count input spacing
- ReceiveScreen: reduced PO header and receive card spacing
- LoginScreen: server URL moved to modal popup (was inline toggle), render guard for duplicate mount prevention
- ActiveBatchBanner: layout fixes for C6000 small screen
- Mobile version updated to v0.9.8

### Code Quality
- New `constants.py` with named constants for all status strings (PO, SO, batch, task, count, adjustment, audit action, bin type, role)
- All 12 route files + `picking_service.py` refactored from hardcoded string literals (`'OPEN'`, `'PICKED'`, `'PENDING'`, etc.) to imported constants  -  eliminates typo risk across 100+ status comparisons

### Data
- Renamed 11 branded items to generic descriptions (e.g. "Orvis Clearwater Rod 9ft" → "9ft 5wt Fly Rod"); fly pattern names kept as-is (not trademarked)
- Added `SKIP_SEED` environment variable: `SKIP_SEED=true` creates only admin user + default warehouse + default bins (no demo data); seed script converted to shell wrapper (`db/seed.sh`)

## [v0.9.7] - 2026-04-10

### Repeat Offender Fixes (8 bugs, 14 new tests)
- Admin login: 401 handler no longer redirects during login attempt, preserving username field (#12)
- Item weight: `save()` now sends `weight_lbs` correctly to API (#19)
- Audit log: batch-resolves bin_id/item_id/so_id/po_id to human-readable names (bin codes, SKUs, SO/PO numbers) (#20)
- Receiving bin filter: added `bin_type` query param to `/admin/bins` endpoint (#21)
- Settings unsaved warning: `useBlocker` from react-router-dom v7 replaces manual navigation guard (#22)
- Warehouse delete: hard DELETE with safety checks (bins, zones, inventory) replaces soft-deactivate (#23)
- Login version pin: absolute positioning pinned to bottom of screen (#26)
- Splash double title: removed splash image from app.json (#27)

### Handheld Functional (5 bugs, 2 new tests)
- Cancel receiving: new `/api/receiving/cancel` endpoint reverses receipts, PO line quantities, and inventory; ReceiveScreen tracks session receipt IDs (#2)
- Put-away quantity tracking: remaining qty updates per item instead of removing from queue, green checkmark when fully put away (#3)
- PagedList scroll: changed container from View to ScrollView (#4)
- Over-receive popup: shows warning only once per item per session (#5)
- PICKED SO routing: scanned PICKED orders now navigate to Ship screen (#10)

### Handheld UI (7 bugs)
- Settings menu: centered overlay with scrollable scan config (#1)
- Renamed "Wave picking" to "Pick orders" on home screen (#6)
- Double pick confirmation: auto-submits batch when all tasks complete, eliminated intermediate "Round Complete" view (#7)
- Replaced all 9 `Alert.alert` calls with styled React Native modals across HomeScreen and ReceiveScreen (#8)
- Warehouse selector: `TouchableOpacity` → `Pressable` for single-tap selection on Android, added overlay dismiss (#9)
- Removed badge numbers from home screen operation cards (#11)
- Scroll position: added `useScrollToTop` from React Navigation to all 7 scrollable screens (#14)

### Admin Panel (8 bugs)
- Cycle count approval: per-bin Submit/Approve All/Reject All buttons replace single global submit (#13)
- User management: Delete (hard) replaces Deactivate, with styled confirmation modal (#15)
- Create SO: full form on Picking page with so_number, warehouse, customer name/phone/address, ship method/address, order lines with item picker (#16)
- Item management: view modal is read-only, edit modal now has Delete/Archive buttons (#17)
- Delete item: styled confirmation popup replaces `confirm()` (#18)
- SO clickable: row click on Picking/Packing/Shipping pages opens customer detail modal (#24)
- Customer fields: added customer_phone and customer_address to sales order list API response (#25)

### EAS Build
- AsyncStorage URL: new `initApiUrl()` preloads saved server URL before any screens render; AuthProvider awaits it during loading phase

### Stats
- 277 tests passing (16 new)
- 29 files changed, +1,105 / -212 lines

## [v0.9.6] - 2026-04-09

### Fixed
- Scan hardening, cycle count approval, put-away reorder, manual picking, admin UX overhaul, CSV templates, role simplification

## [v0.9.5] - 2026-04-08

### Admin Panel
- Cycle count approval page: review pending adjustments per item, approve/reject individually, apply approved changes to inventory
- Inventory page: sortable columns by clicking headers (SKU, item name, bin, zone, quantities)
- Item edit: Delete button (hard delete with confirmation, blocked if order history) and Archive button (soft delete, restorable)
- Items page: filter dropdown for Active, Archived, or All items
- Purchase orders: dedicated page showing all POs with status filter, clickable rows with Ordered/Received line detail
- User creation: warehouse checkbox list (multi-warehouse assignment), simplified roles (Admin/User), mobile module access checkboxes (Pick, Pack, Ship, Receive, Put-Away, Count, Transfer)
- User role enforcement: USER role shows "Not authorized, contact admin" on admin panel login
- Warehouse management page: create, edit, delete warehouses
- Settings: batch Save button replaces auto-save, "Unsaved changes" indicator, browser beforeunload warning
- Admin panel version updated to 0.9.5

### Mobile (Batch 1  -  Scan Debug)
- Added `[SCAN_DEBUG]` logging to every scan handler across all screens
- Added `[API_DEBUG]` request/response logging to API client
- ScanInput: removed 300ms auto-submit timer (caused partial barcodes on C6000), added processing lock, improved whitespace/CR sanitization
- All scan handlers: process only on Enter/Submit, trim `\r\n\s`, ignore empty, disable during processing

### Mobile (Batch 2  -  Features)
- Put-away: replaced forced sequential flow with scrollable item list (scan or tap any item)
- Pick walk: item detail modal now has PICK + CLOSE buttons side by side for manual picking
- Pick walk: replaced Alert.alert cancel with styled app modal (white card, 12px radius, tan border)
- Pick walk: fixed NEXT ITEM PREVIEW  -  wrong API URL, stale task list, forward-scan logic for next PENDING task, "LAST ITEM IN BATCH" on final item
- Cycle count architecture: removed auto-adjustment of inventory on variance  -  creates PENDING audit records instead
- Cycle count: support for unexpected items (items found during count not in snapshot), flagged with "NEW" badge
- Cycle count: blind count mode respects `count_show_expected` setting from admin
- Transfer: X clear buttons on FROM BIN and TO BIN fields to correct mis-scans

### CSV Templates
- Added `docs/templates/` with 4 import templates: items, purchase orders, sales orders, bins (3 example rows each)
- CSV import now supports purchase orders and sales orders (SKU-based line matching, auto-creates PO/SO headers)
- "Download Template" link next to each import type selector

### Database
- Migration 013: `warehouse_ids INT[]` on users for multi-warehouse, role simplification (ADMIN/USER), default mobile module access
- New endpoints: `GET/POST /api/admin/adjustments/pending|review`, `POST /api/admin/items/:id/archive`, `DELETE /api/admin/warehouses/:id`

### Stats
- 261 tests passing

## [v0.9.4] - 2026-04-08

### Refactored
- Extracted `inventory_service.py` with `add_inventory()` and `move_inventory()`  -  inventory math now lives in one place instead of 3 route files
- Created `@with_db` decorator  -  eliminates manual db session boilerplate from all 10 route files + 43 admin routes
- Split 1,925-line `admin.py` monolith into 4 focused modules: `admin_warehouse.py`, `admin_items.py`, `admin_orders.py`, `admin_users.py`
- Extracted shared mobile StyleSheets: `screenStyles`, `buttonStyles`, `modalStyles`, `listStyles`, `doneStyles`  -  removed ~360 lines of duplicate styles across 12 screens
- Created `useScreenError` hook  -  consolidated error + scanDisabled state in 10 screens
- Created `ScreenHeader` component  -  replaced ~20 lines of duplicated header JSX per screen
- Created `ModeSelector` component  -  reusable Standard/Turbo toggle for Receive and Count screens
- Added `ActivityIndicator` loading states to HomeScreen and PickWalkScreen

### Fixed
- ReceiveScreen hardcoded `warehouse_id=1` now uses auth context (multi-warehouse support)
- Removed `console.log` statements from ScanInput and HomeScreen

### Stats
- 261 tests passing
- Net: +2,081 / -12,918 lines (mostly deduplication)

## [v0.9.3] - 2026-04-08

### Fixed
- UI revamp: tan cards, 12px radius, accent stripes, NEXT pick preview, blind cycle counts, carrier picker, password clear on bad login

## [v0.9.2] - 2026-04-08

### Fixed
- Test suite refactored from per-test TRUNCATE+reseed to transaction rollback (261 tests in ~4.3s, fixes 365-min CI deadlock)
- ScanInput auto-refocus every 500ms and auto-submit after 100ms pause for C6000 hardware scanner
- Expanded ignored keys in ScanInput (F1-F12, Tab, Escape, GoBack)

### Added
- Short pick admin reporting endpoint (GET /api/admin/short-picks) with SKU, bin, expected/picked/shortage, picker, timestamp
- Short pick count on dashboard pipeline (7d rolling, red when > 0)
- Pick walk item detail modal  -  tap any item card for SKU, UPC, bin, zone, qty, contributing orders
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
