import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import PageHeader from '../components/PageHeader.jsx';

// avid-overhaul-mk1 P9.1: dashboard view of staging bins for the
// supervisor. Each staging bin shows its SKU count + total qty; the
// row expands to the per-item breakdown. CSV exports cover both the
// single-bin view (workpaper for the operator walking that aisle) and
// the warehouse-wide view (reconciliation / inventory analysis).

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
  const [expanded, setExpanded] = useState({});
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

  function toggle(binId) {
    setExpanded((e) => ({ ...e, [binId]: !e[binId] }));
  }

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

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {bins.map((b) => {
          const isEmpty = b.sku_count === 0;
          // P9.4: empty staging bins still render so the supervisor
          // sees the full layout, but they are non-expandable and
          // visually muted so the worklist signal stays clear.
          const open = !isEmpty && !!expanded[b.bin_id];
          return (
            <div
              key={b.bin_id}
              className="card"
              style={{ padding: 0, opacity: isEmpty ? 0.55 : 1 }}
            >
              <div
                style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  padding: '10px 14px',
                  cursor: isEmpty ? 'default' : 'pointer',
                  borderBottom: open ? '1px solid var(--border-dark)' : 'none',
                }}
                onClick={isEmpty ? undefined : () => toggle(b.bin_id)}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                    {isEmpty ? '·' : (open ? '▾' : '▸')}
                  </span>
                  <span className="mono" style={{ fontSize: 14, fontWeight: 600 }}>
                    {b.bin_code}
                  </span>
                  <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                    {isEmpty
                      ? 'empty'
                      : `${b.sku_count} ${b.sku_count === 1 ? 'SKU' : 'SKUs'} - ${b.total_qty} units`}
                  </span>
                </div>
                {!isEmpty && (
                  <button
                    className="btn btn-sm"
                    onClick={(e) => { e.stopPropagation(); exportBin(b); }}
                    title={`Export ${b.bin_code} to CSV`}
                  >
                    CSV
                  </button>
                )}
              </div>
              {open && (
                <div style={{ padding: '10px 14px' }}>
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
                      {b.items.map((it) => (
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
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
