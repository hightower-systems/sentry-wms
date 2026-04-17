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
