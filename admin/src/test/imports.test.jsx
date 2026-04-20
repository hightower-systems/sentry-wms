/**
 * v1.4.2 #91: CSV import templates must include every field the
 * /api/admin/import/<type> endpoint treats as required for the row to
 * land in the database.
 *
 * Mismatches surfaced as "Missing required field: warehouse_id" row
 * errors on PO/SO imports and "Zone 'X' not found" on Bin imports from
 * a freshly-downloaded template. This suite keeps each template's
 * header row in lockstep with the corresponding pydantic import schema
 * (api/schemas/csv_import.py) so a future schema change fails a test
 * instead of a user's import.
 */

import { describe, it, expect } from 'vitest';
import { CSV_TEMPLATES } from '../pages/Imports.jsx';

function headers(templateKey) {
  const firstLine = CSV_TEMPLATES[templateKey].split('\n')[0];
  return firstLine.split(',').map((h) => h.trim());
}

describe('CSV import templates', () => {
  it('items template includes the sku the schema requires and the category+weight the UI hints at', () => {
    const cols = headers('items');
    expect(cols).toContain('sku');
    expect(cols).toContain('name');
    expect(cols).toContain('category');
    expect(cols).toContain('weight');
    expect(cols).toContain('upc');
    expect(cols).toContain('default_bin');
    expect(cols).toContain('quantity');
  });

  it('bins template carries zone (resolved to zone_id by the server) and warehouse_id', () => {
    const cols = headers('bins');
    expect(cols).toContain('bin_code');
    expect(cols).toContain('bin_barcode');
    expect(cols).toContain('zone');
    expect(cols).toContain('warehouse_id');
    expect(cols).toContain('bin_type');
    expect(cols).toContain('pick_sequence');
    expect(cols).toContain('putaway_sequence');
  });

  it('purchase-orders template includes warehouse_id, required by the import helper', () => {
    const cols = headers('purchase-orders');
    expect(cols).toContain('po_number');
    expect(cols).toContain('warehouse_id');
    expect(cols).toContain('sku');
    expect(cols).toContain('quantity');
    expect(cols).toContain('vendor');
    expect(cols).toContain('expected_date');
  });

  it('sales-orders template includes warehouse_id, required by the import helper', () => {
    const cols = headers('sales-orders');
    expect(cols).toContain('so_number');
    expect(cols).toContain('warehouse_id');
    expect(cols).toContain('sku');
    expect(cols).toContain('quantity');
    expect(cols).toContain('customer');
    expect(cols).toContain('customer_address');
  });

  it('every template has at least one example row after the header', () => {
    for (const key of Object.keys(CSV_TEMPLATES)) {
      const lines = CSV_TEMPLATES[key].split('\n').filter(Boolean);
      expect(lines.length).toBeGreaterThan(1);
    }
  });
});
