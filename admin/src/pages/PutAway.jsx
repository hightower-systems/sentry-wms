import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

// avid-overhaul-mk1 P9.1 / P9.4: dashboard view of staging bins for
// the supervisor. Grid of rounded-rectangle bin tiles, alphabetised,
// with a red border for bins that have items awaiting put-away and a
// grey border for empty ones. Clicking a populated tile opens the
// item breakdown in a modal with a per-bin CSV export; an all-data
// CSV sits in the page header. Auto-fit grid means the row count
// flexes 8-12+ wide depending on viewport.

function csvEscape(v) {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function downloadCsv(filename, headerRow, dataRows) {
  const lines = [headerRow.join(',')];
  for (const r of dataRows) lines.push(r.map(csvEscape).join(','));
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export default function PutAway() {
  const { warehouseId } = useWarehouse();
  const [bins, setBins] = useState([]);
  const [focusedBin, setFocusedBin] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!warehouseId) return;
    setLoading(true);
    api.get(`/putaway/staging-summary/${warehouseId}`).then(async (res) => {
      setLoading(false);
      if (!res?.ok) return;
      const data = await res.json();
      setBins(data.bins || []);
    }).catch(() => setLoading(false));
  }, [warehouseId]);

  function exportAll() {
    const header = ['Bin', 'SKU', 'Item Name', 'UPC', 'Quantity', 'Suggested Bin', 'Lot'];
    const rows = [];
    for (const b of bins) {
      for (const item of b.items) {
        rows.push([
          b.bin_code, item.sku, item.item_name, item.upc || '',
          item.quantity_on_hand, item.suggested_bin || '', item.lot_number || '',
        ]);
      }
    }
    const stamp = new Date().toISOString().slice(0, 10);
    downloadCsv(`putaway-staging-wh${warehouseId}-${stamp}.csv`, header, rows);
  }

  function exportBin(bin) {
    const header = ['SKU', 'Item Name', 'UPC', 'Quantity', 'Suggested Bin', 'Lot'];
    const rows = bin.items.map((item) => [
      item.sku, item.item_name, item.upc || '',
      item.quantity_on_hand, item.suggested_bin || '', item.lot_number || '',
    ]);
    const stamp = new Date().toISOString().slice(0, 10);
    downloadCsv(`putaway-${bin.bin_code}-${stamp}.csv`, header, rows);
  }

  const totalSkus = bins.reduce((acc, b) => acc + b.sku_count, 0);
  const totalQty = bins.reduce((acc, b) => acc + b.total_qty, 0);
  const binsWithItems = bins.reduce((acc, b) => acc + (b.sku_count > 0 ? 1 : 0), 0);

  return (
    <div>
      <PageHeader title="Put-Away">
        <button
          className="btn"
          onClick={exportAll}
          disabled={bins.length === 0}
          title="Export every staging bin and its items to CSV"
        >
          Export All (CSV)
        </button>
      </PageHeader>

      <div style={{
        display: 'flex', gap: 16, marginBottom: 16, fontSize: 13,
        color: 'var(--text-secondary)',
      }}>
        <span>
          <strong style={{ color: 'var(--text)' }}>{binsWithItems}</strong>
          {' / '}{bins.length} staging bins with items
        </span>
        <span><strong style={{ color: 'var(--text)' }}>{totalSkus}</strong> total SKU rows</span>
        <span><strong style={{ color: 'var(--text)' }}>{totalQty}</strong> total units</span>
      </div>

      {loading && (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Loading…</p>
      )}
      {!loading && bins.length === 0 && (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          No staging bins exist in this warehouse.
        </p>
      )}

      <div className="putaway-grid">
        {bins.map((b) => {
          const isEmpty = b.sku_count === 0;
          return (
            <button
              key={b.bin_id}
              type="button"
              className={`putaway-tile ${isEmpty ? 'empty' : 'has-items'}`}
              onClick={isEmpty ? undefined : () => setFocusedBin(b)}
              disabled={isEmpty}
              title={isEmpty
                ? `${b.bin_code} - empty`
                : `${b.bin_code} - ${b.sku_count} SKU${b.sku_count === 1 ? '' : 's'}, ${b.total_qty} units`}
            >
              <div className="putaway-tile-label">{b.bin_code}</div>
              <div className="putaway-tile-count">
                {isEmpty ? 'empty' : `${b.sku_count} SKU${b.sku_count === 1 ? '' : 's'}`}
              </div>
            </button>
          );
        })}
      </div>

      {focusedBin && (
        <Modal
          title={`${focusedBin.bin_code} - ${focusedBin.sku_count} SKUs, ${focusedBin.total_qty} units`}
          onClose={() => setFocusedBin(null)}
          size="wide"
          footer={
            <>
              <button className="btn" onClick={() => exportBin(focusedBin)}>
                Export {focusedBin.bin_code} (CSV)
              </button>
              <button className="btn btn-primary" onClick={() => setFocusedBin(null)}>Close</button>
            </>
          }
        >
          <table className="lines-table">
            <thead>
              <tr>
                <th>SKU</th>
                <th>Item Name</th>
                <th>UPC</th>
                <th style={{ textAlign: 'right' }}>Qty</th>
                <th>Suggested Bin</th>
                <th>Lot</th>
              </tr>
            </thead>
            <tbody>
              {focusedBin.items.map((it) => (
                <tr key={it.inventory_id}>
                  <td className="mono">{it.sku}</td>
                  <td style={{ color: 'var(--text-secondary)' }}>{it.item_name}</td>
                  <td className="mono">{it.upc || '-'}</td>
                  <td className="mono" style={{ textAlign: 'right' }}>{it.quantity_on_hand}</td>
                  <td className="mono">{it.suggested_bin || '-'}</td>
                  <td className="mono">{it.lot_number || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Modal>
      )}
    </div>
  );
}
