# API Reference

All endpoints are prefixed with `/api`. Authentication uses Bearer JWT tokens in the `Authorization` header.

```
Authorization: Bearer <token>
```

---

## Auth

### POST /api/auth/login

Authenticate and receive a JWT token. Rate limited per-username and per-IP (5 attempts, 15 min lockout).

- **Auth required:** No

**Request body:**

| Field | Type | Required |
|-------|------|----------|
| username | string | yes |
| password | string | yes |

**Response (200):**

```json
{
  "token": "eyJhbGci...",
  "user": {
    "user_id": 1,
    "username": "admin",
    "full_name": "Admin User",
    "role": "ADMIN",
    "warehouse_id": 1,
    "warehouse_ids": [1],
    "allowed_functions": ["receive", "putaway", "pick", "pack", "ship", "count", "transfer"],
    "is_active": true
  }
}
```

**Errors:** 400 (missing fields), 401 (invalid credentials), 429 (account locked)

```bash
curl -X POST http://localhost:5000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "yourpassword"}'
```

---

### GET /api/auth/me

Get current user profile with allowed mobile functions.

- **Auth required:** Yes

**Response (200):**

```json
{
  "user_id": 1,
  "username": "admin",
  "full_name": "Admin User",
  "role": "ADMIN",
  "warehouse_id": 1,
  "allowed_functions": ["receive", "putaway", "pick", "pack", "ship", "count", "transfer"],
  "require_packing": true
}
```

```bash
curl http://localhost:5000/api/auth/me \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/auth/refresh

Refresh an existing JWT token. Re-validates the user is active before issuing a new token.

- **Auth required:** Yes

**Response (200):**

```json
{ "token": "eyJhbGci..." }
```

**Errors:** 401 (account disabled or deleted)

```bash
curl -X POST http://localhost:5000/api/auth/refresh \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/auth/change-password

Change the authenticated user's password. Password must be at least 8 characters with at least one letter and one digit.

- **Auth required:** Yes

**Request body:**

| Field | Type | Required |
|-------|------|----------|
| current_password | string | yes |
| new_password | string | yes |

**Response (200):**

```json
{ "message": "Password changed" }
```

**Errors:** 400 (weak password), 403 (current password incorrect)

```bash
curl -X POST http://localhost:5000/api/auth/change-password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"current_password": "oldpass", "new_password": "newpass123"}'
```

---

## Receiving

### GET /api/receiving/po/{barcode}

Look up a purchase order by barcode or PO number.

- **Auth required:** Yes

**Response (200):**

```json
{
  "purchase_order": {
    "po_id": 1,
    "po_number": "PO-001",
    "vendor_name": "Test Vendor",
    "status": "OPEN",
    "warehouse_id": 1
  },
  "lines": [
    {
      "po_line_id": 1,
      "item_id": 1,
      "sku": "TST-001",
      "item_name": "Test Item",
      "upc": "100000000001",
      "quantity_ordered": 10,
      "quantity_received": 0,
      "quantity_remaining": 10,
      "status": "PENDING"
    }
  ]
}
```

**Errors:** 400 (PO closed), 403 (warehouse access), 404 (not found)

```bash
curl http://localhost:5000/api/receiving/po/PO-001 \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/receiving/receive

Record receipt of items from a PO into specified bins. Validates bin belongs to PO's warehouse.

- **Auth required:** Yes

**Request body:**

| Field | Type | Required |
|-------|------|----------|
| po_id | integer | yes |
| items | array | yes |
| items[].item_id | integer | yes |
| items[].quantity | integer | yes (> 0) |
| items[].bin_id | integer | yes |
| items[].lot_number | string | no |

**Response (200):**

```json
{
  "message": "Receipt submitted successfully",
  "receipt_ids": [1, 2],
  "po_status": "PARTIAL",
  "warnings": []
}
```

**Errors:** 400 (invalid data, over-receipt blocked), 403 (warehouse access), 404 (PO/bin not found)

```bash
curl -X POST http://localhost:5000/api/receiving/receive \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"po_id": 1, "items": [{"item_id": 1, "quantity": 10, "bin_id": 1}]}'
```

---

### POST /api/receiving/cancel

Reverse receipts by receipt IDs. Undoes inventory additions and updates PO line quantities.

- **Auth required:** Yes

**Request body:**

| Field | Type | Required |
|-------|------|----------|
| receipt_ids | array of integers | yes |

**Response (200):**

```json
{ "message": "Cancelled 2 receipt(s)", "reversed": 2 }
```

```bash
curl -X POST http://localhost:5000/api/receiving/cancel \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"receipt_ids": [1, 2]}'
```

---

## Put-Away

### GET /api/putaway/pending/{warehouse_id}

List items in staging bins awaiting put-away.

- **Auth required:** Yes

**Response (200):**

```json
{
  "pending_items": [
    {
      "inventory_id": 1,
      "item_id": 1,
      "sku": "TST-001",
      "item_name": "Test Item",
      "quantity": 10,
      "bin_id": 1,
      "bin_code": "STAGE-01",
      "lot_number": null
    }
  ]
}
```

```bash
curl http://localhost:5000/api/putaway/pending/1 \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET /api/putaway/suggest/{item_id}

Get suggested bin for put-away. Checks preferred bins first, then default bin. Scoped to user's allowed warehouses.

- **Auth required:** Yes

**Response (200):**

```json
{
  "item_id": 1,
  "sku": "TST-001",
  "item_name": "Test Item",
  "preferred_bin": {
    "bin_id": 3,
    "bin_code": "A-01-01",
    "bin_barcode": "BIN-A-01-01",
    "zone_name": "Storage A",
    "priority": 1
  },
  "suggested_bin": { "...same as preferred_bin..." }
}
```

```bash
curl http://localhost:5000/api/putaway/suggest/1 \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/putaway/confirm

Confirm put-away transfer from staging to storage bin. Creates a bin_transfers record.

- **Auth required:** Yes

**Request body:**

| Field | Type | Required |
|-------|------|----------|
| item_id | integer | yes |
| from_bin_id | integer | yes |
| to_bin_id | integer | yes |
| quantity | integer | yes (> 0) |
| lot_number | string | no |

**Response (200):**

```json
{
  "message": "Put-away confirmed",
  "transfer_id": 1,
  "item": "TST-001",
  "from_bin": "STAGE-01",
  "to_bin": "A-01-01",
  "quantity": 10
}
```

```bash
curl -X POST http://localhost:5000/api/putaway/confirm \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"item_id": 1, "from_bin_id": 1, "to_bin_id": 3, "quantity": 10}'
```

---

### POST /api/putaway/update-preferred

Set or update the preferred bin for an item.

- **Auth required:** Yes

**Request body:**

| Field | Type | Required |
|-------|------|----------|
| item_id | integer | yes |
| bin_id | integer | yes |
| set_as_primary | boolean | no (default: true) |

**Response (200):**

```json
{
  "message": "Preferred bin for TST-001 set to A-01-01",
  "item_id": 1,
  "bin_id": 3,
  "bin_code": "A-01-01"
}
```

```bash
curl -X POST http://localhost:5000/api/putaway/update-preferred \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"item_id": 1, "bin_id": 3}'
```

---

## Picking

### GET /api/picking/active-batch

Get the current user's active pick batch.

- **Auth required:** Yes

**Response (200):**

```json
{
  "active": true,
  "batch_id": 1,
  "total_picks": 5,
  "completed_picks": 2,
  "total_orders": 3,
  "created_at": "2026-04-14T12:00:00"
}
```

```bash
curl http://localhost:5000/api/picking/active-batch \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/picking/wave-validate

Validate a single SO barcode before adding to a wave batch.

- **Auth required:** Yes

**Request body:** `so_barcode` (string), `warehouse_id` (integer)

**Errors:** 400 (invalid SO), 404 (not found), 409 (already in active batch)

```bash
curl -X POST http://localhost:5000/api/picking/wave-validate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"so_barcode": "SO-001", "warehouse_id": 1}'
```

---

### POST /api/picking/wave-create

Create a wave pick batch from SO IDs. Combines identical items across orders for efficient picking.

- **Auth required:** Yes

**Request body:** `so_ids` (array of integers), `warehouse_id` (integer)

**Response (200):**

```json
{
  "batch_id": 1,
  "batch_status": "IN_PROGRESS",
  "total_orders": 3,
  "total_picks": 5
}
```

```bash
curl -X POST http://localhost:5000/api/picking/wave-create \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"so_ids": [1, 2, 3], "warehouse_id": 1}'
```

---

### POST /api/picking/create-batch

Create a pick batch from SO identifiers (numbers or barcodes).

- **Auth required:** Yes

**Request body:** `so_identifiers` (array of strings), `warehouse_id` (integer)

```bash
curl -X POST http://localhost:5000/api/picking/create-batch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"so_identifiers": ["SO-001", "SO-002"], "warehouse_id": 1}'
```

---

### GET /api/picking/batch/{batch_id}

Get batch details with all pick tasks.

- **Auth required:** Yes

```bash
curl http://localhost:5000/api/picking/batch/1 \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET /api/picking/batch/{batch_id}/next

Get the next pending pick task. Returns `{"message": "All tasks complete"}` when done.

- **Auth required:** Yes

```bash
curl http://localhost:5000/api/picking/batch/1/next \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/picking/confirm

Confirm a pick task. Validates scanned barcode matches the item.

- **Auth required:** Yes

**Request body:**

| Field | Type | Required |
|-------|------|----------|
| pick_task_id | integer | yes |
| scanned_barcode | string | yes |
| quantity_picked | integer | yes (> 0) |

**Errors:** 400 (barcode mismatch, invalid quantity)

```bash
curl -X POST http://localhost:5000/api/picking/confirm \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pick_task_id": 1, "scanned_barcode": "100000000001", "quantity_picked": 2}'
```

---

### POST /api/picking/short

Report a short pick.

- **Auth required:** Yes

**Request body:** `pick_task_id` (integer), `quantity_available` (integer, default 0)

```bash
curl -X POST http://localhost:5000/api/picking/short \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pick_task_id": 1, "quantity_available": 0}'
```

---

### POST /api/picking/complete-batch

Mark a batch as complete. All tasks must be picked, shorted, or skipped.

- **Auth required:** Yes

**Request body:** `batch_id` (integer)

```bash
curl -X POST http://localhost:5000/api/picking/complete-batch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"batch_id": 1}'
```

---

### POST /api/picking/cancel-batch

Cancel a batch. Releases allocated inventory and resets SO statuses to OPEN.

- **Auth required:** Yes

**Request body:** `batch_id` (integer)

```bash
curl -X POST http://localhost:5000/api/picking/cancel-batch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"batch_id": 1}'
```

---

## Packing

### GET /api/packing/order/{barcode}

Load an order for packing. Must be in PICKED status.

- **Auth required:** Yes

**Response (200):**

```json
{
  "sales_order": { "so_id": 1, "so_number": "SO-001", "status": "PICKED" },
  "lines": [
    {
      "sku": "TST-001",
      "quantity_picked": 2,
      "quantity_packed": 0,
      "pack_verified": false
    }
  ],
  "calculated_weight_lbs": 1.0,
  "total_items": 2,
  "items_verified": 0
}
```

**Errors:** 400 (not PICKED), 404 (not found)

```bash
curl http://localhost:5000/api/packing/order/SO-001 \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/packing/verify

Scan and verify an item during packing.

- **Auth required:** Yes

**Request body:** `so_id` (integer), `scanned_barcode` (string), `quantity` (integer, default 1)

**Errors:** 400 (over-pack), 404 (item not on order)

```bash
curl -X POST http://localhost:5000/api/packing/verify \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"so_id": 1, "scanned_barcode": "100000000001"}'
```

---

### POST /api/packing/complete

Mark an order as fully packed. All lines must be verified.

- **Auth required:** Yes

**Request body:** `so_id` (integer)

**Errors:** 400 (not all items verified)

```bash
curl -X POST http://localhost:5000/api/packing/complete \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"so_id": 1}'
```

---

## Shipping

### GET /api/shipping/order/{barcode}

Load an order for shipping. Respects the `require_packing_before_shipping` setting.

- **Auth required:** Yes

```bash
curl http://localhost:5000/api/shipping/order/SO-001 \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/shipping/fulfill

Record a shipment. Creates fulfillment records and updates SO to SHIPPED.

- **Auth required:** Yes

**Request body:**

| Field | Type | Required |
|-------|------|----------|
| so_id | integer | yes |
| tracking_number | string | yes (max 255) |
| carrier | string | yes (max 100) |
| ship_method | string | no |

**Response (200):**

```json
{
  "message": "Shipment fulfilled",
  "fulfillment_id": 1,
  "so_number": "SO-001",
  "tracking_number": "1Z999AA10123456784",
  "carrier": "UPS",
  "lines_shipped": 1,
  "total_quantity": 2
}
```

```bash
curl -X POST http://localhost:5000/api/shipping/fulfill \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"so_id": 1, "tracking_number": "1Z999AA10123456784", "carrier": "UPS"}'
```

---

## Inventory

### POST /api/inventory/cycle-count/create

Create cycle counts for one or more bins. Snapshots current inventory as expected quantities.

- **Auth required:** Yes

**Request body:** `warehouse_id` (integer), `bin_ids` (array of integers)

**Response (200):**

```json
{
  "counts": [
    {
      "count_id": 1,
      "bin_id": 3,
      "bin_code": "A-01-01",
      "status": "PENDING",
      "lines": 2,
      "assigned_to": "admin"
    }
  ]
}
```

```bash
curl -X POST http://localhost:5000/api/inventory/cycle-count/create \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"warehouse_id": 1, "bin_ids": [3, 4]}'
```

---

### GET /api/inventory/cycle-count/{count_id}

Get cycle count details. Respects `count_show_expected` setting for blind counts.

- **Auth required:** Yes

```bash
curl http://localhost:5000/api/inventory/cycle-count/1 \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/inventory/cycle-count/submit

Submit cycle count results. Creates pending inventory adjustments for variances.

- **Auth required:** Yes

**Request body:**

| Field | Type | Required |
|-------|------|----------|
| count_id | integer | yes |
| lines | array | yes |
| lines[].count_line_id | integer | yes |
| lines[].counted_quantity | integer | yes (>= 0) |
| lines[].unexpected | boolean | no |
| lines[].item_id | integer | yes if unexpected |

**Response (200):**

```json
{
  "status": "VARIANCE",
  "summary": {
    "total_lines": 2,
    "lines_with_variance": 1,
    "adjustments": [
      { "sku": "TST-001", "expected": 50, "counted": 45, "variance": -5, "adjustment_id": 1 }
    ]
  }
}
```

```bash
curl -X POST http://localhost:5000/api/inventory/cycle-count/submit \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"count_id": 1, "lines": [{"count_line_id": 1, "counted_quantity": 45}]}'
```

---

## Transfers

### POST /api/transfers/move

Move items between bins within the same warehouse.

- **Auth required:** Yes

**Request body:**

| Field | Type | Required |
|-------|------|----------|
| item_id | integer | yes |
| from_bin_id | integer | yes |
| to_bin_id | integer | yes |
| quantity | integer | yes (> 0) |
| reason | string | no |
| lot_number | string | no |

**Errors:** 400 (cross-warehouse, same bin, insufficient qty)

```bash
curl -X POST http://localhost:5000/api/transfers/move \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"item_id": 1, "from_bin_id": 3, "to_bin_id": 4, "quantity": 10}'
```

---

## Warehouses

### GET /api/warehouses/list

List all active warehouses. Used by the mobile app for post-login warehouse selection.

- **Auth required:** Yes

**Response (200):**

```json
{
  "warehouses": [
    { "id": 1, "name": "Apartment Lab", "code": "APT-LAB" }
  ]
}
```

```bash
curl http://localhost:5000/api/warehouses/list \
  -H "Authorization: Bearer $TOKEN"
```

---

## Lookups

### GET /api/lookup/item/{barcode}

Look up an item by UPC, SKU, or barcode alias. Returns inventory locations scoped to user's warehouses.

- **Auth required:** Yes

```bash
curl http://localhost:5000/api/lookup/item/100000000001 \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET /api/lookup/bin/{barcode}

Look up a bin by barcode or bin code with contents.

- **Auth required:** Yes

```bash
curl http://localhost:5000/api/lookup/bin/A-01-01 \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET /api/lookup/so/{barcode}

Look up a sales order by barcode or SO number with line-by-line fulfillment progress.

- **Auth required:** Yes

```bash
curl http://localhost:5000/api/lookup/so/SO-001 \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET /api/lookup/item/search?q={query}

Search items by SKU, name, or UPC. Case-insensitive. Returns up to 50 results.

- **Auth required:** Yes

```bash
curl "http://localhost:5000/api/lookup/item/search?q=fly" \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET /api/lookup/bin/search?q={query}

Search bins by code or barcode. Scoped to user's warehouses. Returns up to 50 results.

- **Auth required:** Yes

```bash
curl "http://localhost:5000/api/lookup/bin/search?q=A-01" \
  -H "Authorization: Bearer $TOKEN"
```

---

## Admin Endpoints

All admin endpoints require authentication and the **ADMIN** role.

---

### Admin - Warehouses

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/warehouses` | List warehouses (paginated) |
| GET | `/api/admin/warehouses/{id}` | Get warehouse with zones |
| POST | `/api/admin/warehouses` | Create warehouse (`warehouse_code`, `warehouse_name` required) |
| PUT | `/api/admin/warehouses/{id}` | Update warehouse |
| DELETE | `/api/admin/warehouses/{id}` | Delete warehouse (blocked if has inventory/bins/zones) |

---

### Admin - Zones

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/zones` | List zones (filter: `warehouse_id`, paginated) |
| POST | `/api/admin/zones` | Create zone (`warehouse_id`, `zone_code`, `zone_name`, `zone_type` required) |
| PUT | `/api/admin/zones/{id}` | Update zone |

Zone types: `STORAGE`, `RECEIVING`, `STAGING`, `SHIPPING`, `QUALITY`, `DAMAGE`

---

### Admin - Bins

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/bins` | List bins (filter: `warehouse_id`, `zone_id`, `bin_type`, paginated) |
| GET | `/api/admin/bins/{id}` | Get bin with inventory contents |
| POST | `/api/admin/bins` | Create bin (`zone_id`, `warehouse_id`, `bin_code`, `bin_barcode`, `bin_type` required) |
| PUT | `/api/admin/bins/{id}` | Update bin |

Bin types: `Pickable`, `PickableStaging`, `Staging`

---

### Admin - Items

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/items` | List items (filter: `category`, `active`, `q` search, paginated) |
| GET | `/api/admin/items/{id}` | Get item with inventory and preferred bins |
| POST | `/api/admin/items` | Create item (`sku`, `item_name` required) |
| PUT | `/api/admin/items/{id}` | Update item |
| POST | `/api/admin/items/{id}/archive` | Toggle archive/restore |
| DELETE | `/api/admin/items/{id}` | Hard delete (blocked if inventory/order history) |

---

### Admin - Inventory

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/inventory` | Inventory overview (filter: `warehouse_id`, `item_id`, paginated) |

---

### Admin - Preferred Bins

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/preferred-bins` | List preferred bins (filter: `item_id`, `bin_id`, `q` search) |
| POST | `/api/admin/preferred-bins` | Create/update preferred bin (`item_id`, `bin_id` required) |
| PUT | `/api/admin/preferred-bins/{id}` | Update priority |
| DELETE | `/api/admin/preferred-bins/{id}` | Delete preferred bin |

---

### Admin - Purchase Orders

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/purchase-orders` | List POs (filter: `status`, `warehouse_id`, paginated) |
| GET | `/api/admin/purchase-orders/{id}` | Get PO with lines |
| POST | `/api/admin/purchase-orders` | Create PO with lines (`po_number`, `warehouse_id`, `lines` required) |
| PUT | `/api/admin/purchase-orders/{id}` | Update PO (OPEN only) |
| POST | `/api/admin/purchase-orders/{id}/close` | Close PO |

---

### Admin - Sales Orders

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/sales-orders` | List SOs (filter: `status`, `warehouse_id`, paginated) |
| GET | `/api/admin/sales-orders/{id}` | Get SO with lines |
| POST | `/api/admin/sales-orders` | Create SO with lines (`so_number`, `warehouse_id`, `lines` required) |
| PUT | `/api/admin/sales-orders/{id}` | Update SO (OPEN only) |
| POST | `/api/admin/sales-orders/{id}/cancel` | Cancel SO (releases allocated inventory) |

---

### Admin - Users

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/users` | List users (paginated) |
| POST | `/api/admin/users` | Create user (`username`, `password`, `full_name`, `role` required) |
| PUT | `/api/admin/users/{id}` | Update user (safeguards prevent self-demotion, last-admin removal) |
| DELETE | `/api/admin/users/{id}` | Delete user (cannot delete self or last admin) |

Roles: `ADMIN`, `USER`

Allowed functions: `pick`, `pack`, `ship`, `receive`, `putaway`, `count`, `transfer`

---

### Admin - Import

**POST /api/admin/import/{entity_type}**

Bulk import records. Max 5000 records per request.

Entity types: `items`, `bins`, `purchase-orders`, `sales-orders`

**Request body:**

```json
{
  "records": [
    {"sku": "NEW-001", "item_name": "New Item"},
    {"sku": "NEW-002", "item_name": "Another Item"}
  ]
}
```

**Response (200):**

```json
{
  "message": "Import complete",
  "total": 2,
  "imported": 2,
  "skipped": 0,
  "errors": []
}
```

---

### Admin - Dashboard

**GET /api/admin/dashboard**

Dashboard stats. Optional `warehouse_id` filter.

Returns: `open_pos`, `pending_receipts`, `items_awaiting_putaway`, `open_sos`, `orders_ready_to_pick`, `orders_in_picking`, `ready_to_ship`, `ready_to_pack`, `orders_packed`, `total_skus`, `total_bins`, `low_stock_items`, `short_picks_7d`, `pending_adjustments`, `require_packing`, `recent_activity`.

---

### Admin - Settings

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/settings` | Get all settings |
| GET | `/api/admin/settings/{key}` | Get single setting |
| PUT | `/api/admin/settings` | Update settings (`settings` object with key-value pairs) |

Available settings: `require_packing_before_shipping`, `count_show_expected`, `allow_over_receiving`, `default_receiving_bin`, `require_count_approval_separation`

---

### Admin - Adjustments

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/admin/adjustments/pending` | List pending cycle count adjustments |
| POST | `/api/admin/adjustments/review` | Approve/reject adjustments (`decisions` array) |
| POST | `/api/admin/adjustments/direct` | Direct inventory add/remove (auto-approved) |
| GET | `/api/admin/adjustments/list` | Adjustment history (filter: `warehouse_id`, paginated) |

Self-approval blocked when `require_count_approval_separation` is enabled.

---

### Admin - Cycle Counts

**GET /api/admin/cycle-counts** - List cycle counts with line details (last 200).

---

### Admin - Inter-Warehouse Transfers

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/admin/inter-warehouse-transfer` | Move inventory between warehouses |
| GET | `/api/admin/inter-warehouse-transfers` | Recent transfers (optional `limit`, default 50) |

---

### Admin - Short Picks

**GET /api/admin/short-picks** - Short pick report. Filter by `days` (default 30), `warehouse_id`.

---

### Admin - Audit Log

**GET /api/admin/audit-log** - Paginated audit log. Filter by `action_type`, `user_id`, `start_date`, `end_date`.
