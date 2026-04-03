# Changelog

All notable changes to Sentry WMS will be documented in this file.

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
