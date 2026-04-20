/**
 * v1.4.2 #84: DataTable CSV export must serialize status columns to the
 * underlying enum string, not `[object Object]`.
 *
 * DataTable's exportCSV was calling `col.render(row)` unconditionally for
 * every column. When a render function returns a React element (e.g.
 * `<StatusTag status={r.status} />`), that element coerces to the literal
 * "[object Object]" when joined into a CSV cell. Any admin page whose list
 * renders status via <StatusTag> (Purchase Orders, Sales Orders, Receiving,
 * Picking, Packing, Shipping, Cycle Counts, Dashboard) was affected.
 *
 * The fix teaches exportCSV to: (a) prefer an explicit `csvValue(row)` if
 * the column opts in, (b) use the render output when it is a primitive,
 * and (c) fall back to the raw `row[col.key]` value otherwise.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import DataTable from '../components/DataTable.jsx';

function StatusTag({ status }) {
  return <span data-testid={`status-${status}`}>{status}</span>;
}

function renderTable(columns, data) {
  return render(
    <DataTable
      columns={columns}
      data={data}
      pagination={{ page: 1, pages: 1, total: data.length }}
      onPageChange={() => {}}
    />
  );
}

function captureExportedCsv(button) {
  const capturedCsv = { text: '' };
  const originalBlob = globalThis.Blob;
  globalThis.Blob = class MockBlob {
    constructor(parts) { capturedCsv.text = parts.join(''); }
  };
  const createObjectURL = vi.fn(() => 'blob://csv');
  const revokeObjectURL = vi.fn();
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;

  // HTMLAnchorElement.click() triggers a download in a browser. In JSDOM it
  // is a no-op, which is exactly what we want -- the CSV is already
  // captured from the Blob constructor.
  fireEvent.click(button);

  globalThis.Blob = originalBlob;
  URL.createObjectURL = originalCreate;
  URL.revokeObjectURL = originalRevoke;
  return capturedCsv.text;
}


describe('DataTable CSV export', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('exports the raw status string when render returns JSX', () => {
    const columns = [
      { key: 'po_number', label: 'PO Number' },
      { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    ];
    const data = [
      { po_number: 'PO-001', status: 'OPEN' },
      { po_number: 'PO-002', status: 'RECEIVED' },
    ];
    const { getByText } = renderTable(columns, data);
    const csv = captureExportedCsv(getByText('Export CSV'));

    expect(csv).toContain('Status');
    expect(csv).toContain('OPEN');
    expect(csv).toContain('RECEIVED');
    expect(csv).not.toContain('[object Object]');
  });

  it('preserves primitive render output (derived values)', () => {
    const columns = [
      { key: 'sku', label: 'SKU' },
      {
        key: 'available',
        label: 'Available',
        render: (r) => (r.on_hand || 0) - (r.committed || 0),
      },
    ];
    const data = [{ sku: 'A-1', on_hand: 10, committed: 3 }];
    const { getByText } = renderTable(columns, data);
    const csv = captureExportedCsv(getByText('Export CSV'));

    expect(csv).toContain('7');
    expect(csv).not.toContain('[object Object]');
  });

  it('prefers csvValue when provided, ignoring render JSX', () => {
    const columns = [
      { key: 'sku', label: 'SKU' },
      {
        key: 'status',
        label: 'Status',
        render: (r) => <StatusTag status={r.status} />,
        csvValue: (r) => `STATUS[${r.status}]`,
      },
    ];
    const data = [{ sku: 'A-1', status: 'OPEN' }];
    const { getByText } = renderTable(columns, data);
    const csv = captureExportedCsv(getByText('Export CSV'));

    expect(csv).toContain('STATUS[OPEN]');
    expect(csv).not.toContain('[object Object]');
  });

  it('falls back to raw field when render returns null', () => {
    const columns = [
      { key: 'sku', label: 'SKU' },
      { key: 'status', label: 'Status', render: (r) => (r.status ? null : null) },
    ];
    const data = [{ sku: 'A-1', status: 'CLOSED' }];
    const { getByText } = renderTable(columns, data);
    const csv = captureExportedCsv(getByText('Export CSV'));

    expect(csv).toContain('CLOSED');
  });
});
