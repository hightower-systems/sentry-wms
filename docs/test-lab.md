# Test Lab Setup

How to set up a small-scale test environment to exercise all warehouse flows end to end.

## Hardware

- **Scanner** - Chainway C6000 (or any Android device with Expo Go for dev testing)
- **Label printer** - Zebra ZD421 or similar (optional -- you can print barcodes on paper)
- **Barcode labels** - item UPCs, bin barcodes, PO/SO barcodes

## Network

- All devices on the same local network (WiFi)
- API server running on a machine with a known IP address
- Mobile app connects to `http://<server-ip>:5000`

## Getting Started

### 1. Start Fresh

```bash
cd sentry-wms
docker compose down -v
docker compose up -d
```

This resets the database and loads demo data (20 items, 16 bins, 5 POs, 20 SOs).

### 2. Log In to Admin Panel

Open `http://<server-ip>:8080` in a browser. Fresh installs seed the admin user as `admin` / `admin`; you are forced to change the password on first login.

If you set `ADMIN_PASSWORD` in your `.env`, the seed uses that value instead and skips the forced-change flow -- `docker compose logs db | grep "Admin password"` prints it for you.

### 3. Install the Mobile App

Either sideload the APK via `adb install` or use Expo Go for development:

```bash
cd mobile
npm install
npx expo start --clear
```

On first launch, enter the server URL (`http://<server-ip>:5000`) and log in.

---

## Test Flow Walkthrough

### Step 1: Receive a Purchase Order

1. In the admin panel, go to Settings and create a PO (or use one of the 5 demo POs)
2. On the mobile app, tap **RECEIVE**
3. Scan the PO barcode (or type the PO number)
4. The PO loads with expected line items
5. Scan each item barcode to receive it -- quantity increments per scan in Turbo mode
6. Select a staging bin if prompted
7. Submit the receipt

Verify in the admin panel: PO status updates to PARTIAL or RECEIVED.

### Step 2: Put Away Items

1. On mobile, tap **PUT-AWAY**
2. The pending items list shows everything in staging bins
3. Tap an item or scan its barcode
4. The app suggests a preferred bin (or default bin)
5. Scan the destination bin barcode to confirm
6. Enter quantity and confirm

Verify: item moves from staging to the storage bin in the Inventory page.

### Step 3: Create a Sales Order

1. In the admin panel, go to Settings and create an SO with line items
2. Or use one of the 20 demo SOs

### Step 4: Pick Walk

1. On mobile, tap **PICK**
2. Scan SO barcodes to add orders to the wave (the app validates each one)
3. Tap "Create Batch" to start the pick walk
4. The app shows the first pick task with bin location, item, and quantity
5. Walk to the bin, scan the item barcode to confirm
6. Continue until all tasks are complete
7. Submit the batch

Verify: SO status changes to PICKED.

### Step 5: Pack Verification

1. On mobile, tap **PACK**
2. Scan the SO barcode to load the order
3. Scan each item barcode to verify it matches the pick list
4. The progress bar shows verified vs total items
5. When all items are verified, tap Complete

Verify: SO status changes to PACKED.

### Step 6: Ship

1. On mobile, tap **SHIP**
2. Scan the SO barcode
3. Select a carrier and enter a tracking number
4. Tap Ship

Verify: SO status changes to SHIPPED. Fulfillment records appear in the admin panel.

### Step 7: Cycle Count

1. In the admin panel, go to Cycle Counts and create a count for one or more bins
2. On mobile, tap **COUNT**
3. Scan the bin barcode to load the count
4. Enter the physical quantity for each item
5. Submit the count
6. If variances exist, approve or reject adjustments in the admin panel under Cycle Count Approvals

### Step 8: Transfer

1. On mobile, tap **TRANSFER**
2. Scan the item barcode
3. Scan the source bin (FROM)
4. Scan the destination bin (TO)
5. Enter quantity and confirm

---

## Demo Seed Data

The default seed (`db/seed-apartment-lab.sql`) includes:

- 1 warehouse (APT-LAB) with 6 zones and 16 bins
- 20 items (fly fishing catalog, TST-001 through TST-020)
- 5 purchase orders with varying line counts
- 20 sales orders covering single-item, multi-item, contention, and edge cases
- Inventory pre-loaded in storage bins

Set `SKIP_SEED=true` to start with an empty warehouse and only the admin user.

## Resetting

To wipe everything and start over:

```bash
docker compose down -v
docker compose up -d
```

The `-v` flag removes the PostgreSQL data volume. A fresh seed runs automatically on next startup.
