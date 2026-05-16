import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';
import Modal from '../components/Modal.jsx';

// /api/receiving/receive only accepts OPEN or PARTIAL POs. Mirror
// that constraint in the UI so the operator never sees a 400 from
// the server for a finished PO.
const RECEIVABLE_PO_STATUSES = new Set(['OPEN', 'PARTIAL']);

export default function Receiving() {
  const [pos, setPos] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [statusFilter, setStatusFilter] = useState('active');
  // Bins scoped to the selected PO's warehouse; populated when a PO
  // is opened. Receive form's bin dropdown reads from here.
  const [warehouseBins, setWarehouseBins] = useState([]);
  const [defaultBinId, setDefaultBinId] = useState(null);
  // Per-line draft state: { [po_line_id]: { qty, bin_id, error, saving } }
  const [lineDrafts, setLineDrafts] = useState({});

  useEffect(() => {
    loadPOs();
  }, [statusFilter]);

  async function loadPOs() {
    if (statusFilter === 'active') {
      const [openRes, partialRes] = await Promise.all([
        api.get('/admin/purchase-orders?status=OPEN&per_page=50'),
        api.get('/admin/purchase-orders?status=PARTIAL&per_page=50'),
      ]);
      const all = [];
      for (const res of [openRes, partialRes]) {
        if (res?.ok) {
          const data = await res.json();
          all.push(...(data.purchase_orders || []));
        }
      }
      setPos(all);
    } else {
      const params = new URLSearchParams({ per_page: 50 });
      if (statusFilter !== 'all') params.set('status', statusFilter);
      const res = await api.get(`/admin/purchase-orders?${params}`);
      if (res?.ok) {
        const data = await res.json();
        setPos(data.purchase_orders || []);
      }
    }
  }

  async function viewPO(po) {
    setSelected(po);
    const id = po.po_id || po.id;
    const res = await api.get(`/admin/purchase-orders/${id}`);
    if (!res?.ok) return;
    const data = await res.json();
    setDetail(data);

    const warehouseId = data.purchase_order?.warehouse_id;
    if (warehouseId) {
      const binsRes = await api.get(
        `/admin/bins?warehouse_id=${warehouseId}&per_page=500`,
      );
      if (binsRes?.ok) {
        const bdata = await binsRes.json();
        const bins = bdata.bins || [];
        setWarehouseBins(bins);
        // Default to the first Staging bin in the warehouse so the
        // common receive-to-staging-then-putaway flow is one click.
        // Fall back to the first bin overall if no Staging bins
        // exist (small / unusual warehouse layouts).
        const staging = bins.find((b) => b.bin_type === 'Staging');
        const fallback = bins[0];
        setDefaultBinId((staging || fallback)?.bin_id || null);
      } else {
        setWarehouseBins([]);
        setDefaultBinId(null);
      }
    }
    setLineDrafts({});
  }

  function closeDetail() {
    setSelected(null);
    setDetail(null);
    setWarehouseBins([]);
    setDefaultBinId(null);
    setLineDrafts({});
  }

  async function refreshDetail() {
    if (!detail?.purchase_order) return;
    const res = await api.get(`/admin/purchase-orders/${detail.purchase_order.po_id}`);
    if (res?.ok) {
      setDetail(await res.json());
    }
  }

  function updateDraft(lineId, patch) {
    setLineDrafts((d) => ({
      ...d,
      [lineId]: { ...(d[lineId] || {}), ...patch },
    }));
  }

  async function receiveLine(line) {
    const draft = lineDrafts[line.po_line_id] || {};
    const qty = parseInt(draft.qty, 10);
    const binId = parseInt(draft.bin_id ?? defaultBinId, 10);
    if (isNaN(qty) || qty <= 0) {
      updateDraft(line.po_line_id, { error: 'Enter a positive quantity' });
      return;
    }
    if (!binId) {
      updateDraft(line.po_line_id, { error: 'Pick a bin' });
      return;
    }
    updateDraft(line.po_line_id, { saving: true, error: '' });
    const res = await api.post('/receiving/receive', {
      po_id: detail.purchase_order.po_id,
      items: [{ item_id: line.item_id, quantity: qty, bin_id: binId }],
    });
    if (res?.ok) {
      // Clear this line's draft and pull the updated PO so the
      // received counter + status reflect the change.
      setLineDrafts((d) => {
        const next = { ...d };
        delete next[line.po_line_id];
        return next;
      });
      await refreshDetail();
      loadPOs();
    } else {
      const data = await res?.json();
      updateDraft(line.po_line_id, {
        saving: false,
        error: data?.error || 'Failed to receive',
      });
    }
  }

  const columns = [
    { key: 'po_number', label: 'PO Number', mono: true },
    { key: 'vendor_name', label: 'Vendor' },
    { key: 'expected_date', label: 'Expected Date', mono: true, render: (r) => r.expected_date || '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
  ];

  const po = detail?.purchase_order;
  const canReceive = po && RECEIVABLE_PO_STATUSES.has(po.status);

  return (
    <div>
      <PageHeader title="Receiving" />
      <div className="filter-bar">
        <select className="form-select" style={{ width: 140 }} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="active">Open / Partial</option>
          <option value="all">All</option>
          <option value="OPEN">Open</option>
          <option value="PARTIAL">Partial</option>
          <option value="RECEIVED">Received</option>
          <option value="CLOSED">Closed</option>
        </select>
      </div>
      <DataTable columns={columns} data={pos} onRowClick={viewPO} emptyMessage="No purchase orders" />

      {selected && detail && (
        <Modal
          title={`PO ${po?.po_number || selected.po_number}`}
          onClose={closeDetail}
          footer={<button className="btn" onClick={closeDetail}>Close</button>}
          size="wide"
        >
          <section className="section">
            <div className="section-title">PO Summary</div>
            <div className="detail-grid detail-grid-2col" style={{ marginBottom: 0 }}>
              <span className="detail-label">Vendor</span><span>{po?.vendor_name || '-'}</span>
              <span className="detail-label">Status</span><span><StatusTag status={po?.status} /></span>
              <span className="detail-label">Expected Date</span><span className="mono">{po?.expected_date || '-'}</span>
              <span className="detail-label">Warehouse</span><span className="mono">{po?.warehouse_id ?? '-'}</span>
            </div>
          </section>

          <section className="section" style={{ marginBottom: 0 }}>
            <div className="section-title">Line Items</div>
            {!canReceive && (
              <p style={{
                fontSize: 12, color: 'var(--text-secondary)',
                marginBottom: 8, fontStyle: 'italic',
              }}>
                PO status is {po?.status}; receiving is disabled until the
                PO is OPEN or PARTIAL.
              </p>
            )}
            {(detail.lines || []).length > 0 ? (
              <table className="lines-table">
                <thead>
                  <tr>
                    <th>SKU</th>
                    <th>Item</th>
                    <th style={{ textAlign: 'right' }}>Ordered</th>
                    <th style={{ textAlign: 'right' }}>Received</th>
                    <th style={{ textAlign: 'right' }}>Remaining</th>
                    {canReceive && (
                      <>
                        <th style={{ width: 90, textAlign: 'right' }}>Qty</th>
                        <th style={{ width: 220 }}>Bin</th>
                        <th style={{ width: 110 }}></th>
                      </>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {(detail.lines || []).map((l) => {
                    const remaining = (l.quantity_ordered || 0) - (l.quantity_received || 0);
                    const draft = lineDrafts[l.po_line_id] || {};
                    const lineReceivable = canReceive && remaining > 0;
                    return (
                      <tr key={l.po_line_id}>
                        <td className="mono">{l.sku}</td>
                        <td style={{ color: 'var(--text-secondary)' }}>{l.item_name}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_ordered}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_received}</td>
                        <td className="mono" style={{
                          textAlign: 'right',
                          color: remaining > 0 ? 'var(--copper)' : 'var(--text-secondary)',
                          fontWeight: remaining > 0 ? 600 : 400,
                        }}>{remaining}</td>
                        {canReceive && (
                          <>
                            <td style={{ textAlign: 'right' }}>
                              <input
                                type="number" min={1} max={remaining}
                                className="form-input mono"
                                style={{ width: 76, textAlign: 'right', padding: '4px 8px' }}
                                value={draft.qty ?? ''}
                                disabled={!lineReceivable || draft.saving}
                                placeholder={lineReceivable ? '0' : ''}
                                onChange={(e) => updateDraft(l.po_line_id, { qty: e.target.value })}
                              />
                            </td>
                            <td>
                              <select
                                className="form-select"
                                style={{ padding: '4px 8px' }}
                                value={draft.bin_id ?? defaultBinId ?? ''}
                                disabled={!lineReceivable || draft.saving}
                                onChange={(e) => updateDraft(l.po_line_id, { bin_id: e.target.value })}
                              >
                                {warehouseBins.map((b) => (
                                  <option key={b.bin_id} value={b.bin_id}>
                                    {b.bin_code}{b.bin_type ? ` (${b.bin_type})` : ''}
                                  </option>
                                ))}
                              </select>
                            </td>
                            <td style={{ textAlign: 'right' }}>
                              <button
                                className="btn btn-sm btn-primary"
                                disabled={!lineReceivable || draft.saving}
                                onClick={() => receiveLine(l)}
                              >
                                {draft.saving ? 'Receiving...' : 'Receive'}
                              </button>
                            </td>
                          </>
                        )}
                      </tr>
                    );
                  })}
                  {Object.entries(lineDrafts).filter(([, d]) => d.error).map(([lineId, d]) => (
                    <tr key={`err-${lineId}`}>
                      <td colSpan={canReceive ? 8 : 5}>
                        <div className="form-error" style={{ fontSize: 12, padding: '4px 0' }}>
                          Line {lineId}: {d.error}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No line items</p>
            )}
          </section>
        </Modal>
      )}
    </div>
  );
}
