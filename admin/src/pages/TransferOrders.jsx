import { useState, useEffect, useMemo } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

// v1.8.0 (#294) admin UI for transfer orders. Mirrors SalesOrders.jsx
// single-file pattern (DataTable list + click-to-detail modal +
// inline action modals) so the codebase stays uniform.

const STATUS_OPTIONS = [
  'All', 'OPEN', 'PARTIALLY_PICKED', 'AWAITING_APPROVAL',
  'APPROVED', 'CLOSED', 'CANCELLED',
];

const APPROVAL_STATUS_OPTIONS = ['PENDING', 'APPROVED', 'REJECTED'];

function parseCsv(text) {
  // Lightweight client-side CSV parse mirroring Imports.jsx behaviour.
  // First non-empty row is the header; subsequent rows map values into
  // an object keyed by header column. Trims whitespace; permits quoted
  // values for fields containing commas.
  const rows = [];
  const lines = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n');
  let header = null;
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    const cells = [];
    let cur = '';
    let inQuotes = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (c === '"') {
        inQuotes = !inQuotes;
      } else if (c === ',' && !inQuotes) {
        cells.push(cur.trim());
        cur = '';
      } else {
        cur += c;
      }
    }
    cells.push(cur.trim());
    if (!header) {
      header = cells;
    } else {
      const row = {};
      header.forEach((h, idx) => { row[h] = cells[idx] ?? ''; });
      rows.push(row);
    }
  }
  return { header, rows };
}

function downloadShortageCsv(shortages) {
  const headers = ['sku', 'requested_qty', 'available_qty', 'committed_qty', 'shortfall'];
  const lines = [headers.join(',')];
  for (const s of shortages) {
    lines.push(headers.map((h) => String(s[h] ?? '')).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `to-shortages-${new Date().toISOString().slice(0, 19).replace(/[:T-]/g, '')}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

export default function TransferOrders() {
  const [orders, setOrders] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('All');
  const [warehouses, setWarehouses] = useState([]);
  const [sourceFilter, setSourceFilter] = useState('');
  const [destFilter, setDestFilter] = useState('');

  const [selected, setSelected] = useState(null);  // detail modal TO
  const [detailLines, setDetailLines] = useState([]);
  const [detailApprovals, setDetailApprovals] = useState([]);
  const [actionError, setActionError] = useState('');

  const [importing, setImporting] = useState(false);
  const [importForm, setImportForm] = useState({
    source_warehouse_code: '', destination_warehouse_code: '', notes: '',
  });
  const [importRows, setImportRows] = useState([]);
  const [importErrors, setImportErrors] = useState([]);
  const [importMessage, setImportMessage] = useState('');

  const [shortageState, setShortageState] = useState(null);
  // shortageState shape: { to_id, to_number, shortages: [...] }

  const [rejectingApprovalId, setRejectingApprovalId] = useState(null);
  const [rejectionReason, setRejectionReason] = useState('');

  useEffect(() => { loadOrders(); }, [page, statusFilter, sourceFilter, destFilter]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { loadWarehouses(); }, []);

  async function loadWarehouses() {
    const res = await api.get('/admin/warehouses', { silentPermissionDenied: true });
    if (res?.ok) {
      const data = await res.json();
      setWarehouses(data.warehouses || data || []);
    }
  }

  async function loadOrders() {
    const qp = new URLSearchParams({ page: String(page), per_page: '50' });
    if (statusFilter !== 'All') qp.set('status', statusFilter);
    if (sourceFilter) qp.set('source_warehouse_id', sourceFilter);
    if (destFilter) qp.set('destination_warehouse_id', destFilter);
    const res = await api.get(`/admin/transfer-orders?${qp}`);
    if (res?.ok) {
      const data = await res.json();
      setOrders(data.transfer_orders || []);
      setPagination({ page: data.page, pages: data.pages, total: data.total });
    }
  }

  async function viewTO(to) {
    const res = await api.get(`/admin/transfer-orders/${to.to_id}`);
    if (res?.ok) {
      const data = await res.json();
      setSelected(data.transfer_order);
      setDetailLines(data.lines || []);
      setDetailApprovals(data.approvals || []);
      setActionError('');
    }
  }

  async function refreshDetail() {
    if (!selected) return;
    const res = await api.get(`/admin/transfer-orders/${selected.to_id}`);
    if (res?.ok) {
      const data = await res.json();
      setSelected(data.transfer_order);
      setDetailLines(data.lines || []);
      setDetailApprovals(data.approvals || []);
    }
    loadOrders();
  }

  async function cancelTO() {
    if (!selected) return;
    setActionError('');
    const res = await api.post(`/admin/transfer-orders/${selected.to_id}/cancel`, {});
    if (res?.ok) {
      await refreshDetail();
    } else {
      const data = await res?.json();
      setActionError(data?.error || 'Cancel failed');
    }
  }

  async function deleteTO() {
    if (!selected) return;
    setActionError('');
    const res = await api.delete(`/admin/transfer-orders/${selected.to_id}`);
    if (res?.ok) {
      setSelected(null);
      loadOrders();
    } else {
      const data = await res?.json();
      setActionError(data?.error || 'Delete failed');
    }
  }

  async function startPicking() {
    if (!selected) return;
    setActionError('');
    const res = await api.post(`/admin/transfer-orders/${selected.to_id}/start-picking`, {});
    if (res?.ok) {
      const data = await res.json();
      setActionError(`Pick batch ${data.batch_number} created with ${data.tasks_created} task(s)`);
      await refreshDetail();
    } else {
      const data = await res?.json();
      setActionError(data?.error || 'Start picking failed');
    }
  }

  async function shortCloseLine(lineId) {
    if (!selected) return;
    setActionError('');
    const res = await api.post(
      `/admin/transfer-orders/${selected.to_id}/lines/${lineId}/short-close`,
      {},
    );
    if (res?.ok) {
      await refreshDetail();
    } else {
      const data = await res?.json();
      setActionError(data?.error || 'Short-close failed');
    }
  }

  async function approveApproval(approvalId) {
    if (!selected) return;
    setActionError('');
    const res = await api.post(
      `/admin/transfer-orders/${selected.to_id}/approvals/${approvalId}/approve`,
      {},
    );
    if (res?.ok) {
      await refreshDetail();
    } else {
      const data = await res?.json();
      setActionError(data?.error || 'Approve failed');
    }
  }

  async function submitReject() {
    if (!selected || rejectingApprovalId == null) return;
    setActionError('');
    const res = await api.post(
      `/admin/transfer-orders/${selected.to_id}/approvals/${rejectingApprovalId}/reject`,
      { rejection_reason: rejectionReason || null },
    );
    if (res?.ok) {
      setRejectingApprovalId(null);
      setRejectionReason('');
      await refreshDetail();
    } else {
      const data = await res?.json();
      setActionError(data?.error || 'Reject failed');
    }
  }

  function openImport() {
    setImporting(true);
    setImportForm({ source_warehouse_code: '', destination_warehouse_code: '', notes: '' });
    setImportRows([]);
    setImportErrors([]);
    setImportMessage('');
  }

  function onImportFile(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setImportMessage('');
    const reader = new FileReader();
    reader.onload = (ev) => {
      const { rows } = parseCsv(String(ev.target.result || ''));
      // Filter rows missing sku/quantity to prevent server-side noise.
      const cleaned = rows
        .filter((r) => (r.sku || '').trim() !== '')
        .map((r) => ({
          sku: (r.sku || '').trim(),
          quantity: Number(r.quantity || 0),
        }));
      setImportRows(cleaned);
    };
    reader.readAsText(file);
  }

  async function submitImport() {
    setImportErrors([]);
    setImportMessage('');
    if (!importForm.source_warehouse_code || !importForm.destination_warehouse_code) {
      setImportMessage('Source and destination warehouses are required.');
      return;
    }
    if (importRows.length === 0) {
      setImportMessage('Add at least one row before submitting.');
      return;
    }
    const res = await api.post('/admin/transfer-orders/import', {
      source_warehouse_code: importForm.source_warehouse_code,
      destination_warehouse_code: importForm.destination_warehouse_code,
      notes: importForm.notes || null,
      records: importRows,
    });
    if (res?.ok) {
      const data = await res.json();
      setImporting(false);
      if ((data.shortages || []).length > 0) {
        setShortageState({
          to_id: data.to_id,
          to_number: data.to_number,
          shortages: data.shortages,
        });
      }
      loadOrders();
    } else {
      const data = await res?.json();
      if (data?.rows) {
        setImportErrors(data.rows);
      } else {
        setImportMessage(data?.error || data?.detail || 'Import failed');
      }
    }
  }

  async function shortageCreateWithAvailable() {
    setShortageState(null);
  }

  async function shortageCancelTO() {
    if (!shortageState) return;
    await api.delete(`/admin/transfer-orders/${shortageState.to_id}`);
    setShortageState(null);
    loadOrders();
  }

  const warehouseOptions = useMemo(() => {
    return [{ warehouse_id: '', warehouse_code: 'Any', warehouse_name: '' }, ...warehouses];
  }, [warehouses]);

  const columns = [
    { key: 'to_number', label: 'TO Number', mono: true },
    {
      key: 'source_warehouse_id',
      label: 'Source',
      render: (r) => warehouses.find((w) => w.warehouse_id === r.source_warehouse_id)?.warehouse_code || r.source_warehouse_id,
    },
    {
      key: 'destination_warehouse_id',
      label: 'Dest',
      render: (r) => warehouses.find((w) => w.warehouse_id === r.destination_warehouse_id)?.warehouse_code || r.destination_warehouse_id,
    },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'created_by', label: 'Created By' },
    {
      key: 'created_at',
      label: 'Created',
      render: (r) => r.created_at ? new Date(r.created_at).toLocaleDateString() : '-',
    },
  ];

  const thStyle = { textAlign: 'left', padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 };
  const tdStyle = { padding: '6px 8px' };

  const canCancel = selected && ['OPEN', 'PARTIALLY_PICKED'].includes(selected.status);
  const canDelete = selected && selected.status === 'OPEN'
    && detailLines.every((l) => (l.picked_qty || 0) === 0)
    && detailApprovals.length === 0;
  const canStartPicking = selected && ['OPEN', 'PARTIALLY_PICKED'].includes(selected.status)
    && detailLines.some((l) => l.committed_qty > l.picked_qty && l.status !== 'SHORT_CLOSED');

  return (
    <div>
      <PageHeader title="Transfer Orders" />

      <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
        <label style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Status:</label>
        <select className="form-select" value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }} style={{ width: 200 }}>
          {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <label style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Source:</label>
        <select className="form-select" value={sourceFilter} onChange={(e) => { setSourceFilter(e.target.value); setPage(1); }} style={{ width: 140 }}>
          {warehouseOptions.map((w) => (
            <option key={w.warehouse_id || 'any'} value={w.warehouse_id}>{w.warehouse_code}</option>
          ))}
        </select>
        <label style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Dest:</label>
        <select className="form-select" value={destFilter} onChange={(e) => { setDestFilter(e.target.value); setPage(1); }} style={{ width: 140 }}>
          {warehouseOptions.map((w) => (
            <option key={w.warehouse_id || 'any'} value={w.warehouse_id}>{w.warehouse_code}</option>
          ))}
        </select>
        <button className="btn btn-primary" onClick={openImport} style={{ marginLeft: 'auto' }}>
          Import CSV
        </button>
      </div>

      <DataTable
        columns={columns}
        data={orders}
        pagination={pagination}
        onPageChange={setPage}
        onRowClick={viewTO}
        emptyMessage="No transfer orders found"
      />

      {selected && (
        <Modal
          title={`TO ${selected.to_number}`}
          onClose={() => setSelected(null)}
          footer={
            <>
              <button className="btn btn-danger" onClick={deleteTO} disabled={!canDelete}>Delete</button>
              <button className="btn" onClick={cancelTO} disabled={!canCancel}>Cancel TO</button>
              <button className="btn btn-primary" onClick={startPicking} disabled={!canStartPicking}>Start Picking</button>
              <button className="btn" onClick={() => setSelected(null)}>Close</button>
            </>
          }
        >
          {actionError && (
            <div className="form-error" style={{ marginBottom: 12 }}>{actionError}</div>
          )}
          <div className="detail-grid" style={{ marginBottom: 16 }}>
            <span className="detail-label">Source WH</span>
            <span>{warehouses.find((w) => w.warehouse_id === selected.source_warehouse_id)?.warehouse_code || selected.source_warehouse_id}</span>
            <span className="detail-label">Dest WH</span>
            <span>{warehouses.find((w) => w.warehouse_id === selected.destination_warehouse_id)?.warehouse_code || selected.destination_warehouse_id}</span>
            <span className="detail-label">Status</span>
            <span><StatusTag status={selected.status} /></span>
            <span className="detail-label">Created By</span>
            <span>{selected.created_by}</span>
            <span className="detail-label">Notes</span>
            <span>{selected.notes || '-'}</span>
          </div>

          <strong style={{ fontSize: 13 }}>Lines</strong>
          <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse', marginBottom: 16 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                <th style={thStyle}>SKU</th>
                <th style={thStyle}>Item</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Requested</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Committed</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Picked</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Approved</th>
                <th style={thStyle}>Status</th>
                <th style={thStyle}></th>
              </tr>
            </thead>
            <tbody>
              {detailLines.map((l) => (
                <tr key={l.to_line_id} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td className="mono" style={tdStyle}>{l.sku}</td>
                  <td style={{ ...tdStyle, color: 'var(--text-secondary)' }}>{l.item_name}</td>
                  <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.requested_qty}</td>
                  <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.committed_qty}</td>
                  <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.picked_qty}</td>
                  <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.approved_qty}</td>
                  <td style={tdStyle}><StatusTag status={l.status} /></td>
                  <td style={tdStyle}>
                    {l.status !== 'SHORT_CLOSED' && l.status !== 'APPROVED' && l.committed_qty > l.approved_qty && (
                      <button className="btn btn-sm" onClick={() => shortCloseLine(l.to_line_id)}>Short-Close</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {detailApprovals.length > 0 && (
            <>
              <strong style={{ fontSize: 13 }}>Approvals</strong>
              <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse', marginTop: 8 }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--border)' }}>
                    <th style={thStyle}>Submitted By</th>
                    <th style={thStyle}>Submitted At</th>
                    <th style={thStyle}>Status</th>
                    <th style={thStyle}>Approver</th>
                    <th style={thStyle}>Reason</th>
                    <th style={thStyle}></th>
                  </tr>
                </thead>
                <tbody>
                  {detailApprovals.map((a) => (
                    <tr key={a.to_approval_id} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td style={tdStyle}>{a.submitted_by}</td>
                      <td className="mono" style={tdStyle}>{a.submitted_at ? new Date(a.submitted_at).toLocaleString() : '-'}</td>
                      <td style={tdStyle}><StatusTag status={a.status} /></td>
                      <td style={tdStyle}>{a.approved_by || '-'}</td>
                      <td style={{ ...tdStyle, color: 'var(--text-secondary)' }}>{a.rejection_reason || '-'}</td>
                      <td style={tdStyle}>
                        {a.status === 'PENDING' && (
                          <>
                            <button className="btn btn-sm btn-primary" onClick={() => approveApproval(a.to_approval_id)} style={{ marginRight: 6 }}>Approve</button>
                            <button className="btn btn-sm btn-danger" onClick={() => { setRejectingApprovalId(a.to_approval_id); setRejectionReason(''); }}>Reject</button>
                          </>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </Modal>
      )}

      {rejectingApprovalId != null && (
        <Modal
          title="Reject submission"
          onClose={() => setRejectingApprovalId(null)}
          footer={
            <>
              <button className="btn" onClick={() => setRejectingApprovalId(null)}>Cancel</button>
              <button className="btn btn-danger" onClick={submitReject}>Reject</button>
            </>
          }
        >
          <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
            Optional reason. Rejecting leaves source inventory in place
            so the picker can re-pick; short-close the line instead if
            the picks should not return to source.
          </p>
          <textarea
            className="form-input"
            rows={3}
            value={rejectionReason}
            onChange={(e) => setRejectionReason(e.target.value)}
            placeholder="Why are you rejecting?"
          />
        </Modal>
      )}

      {importing && (
        <Modal
          title="Import Transfer Order"
          onClose={() => setImporting(false)}
          footer={
            <>
              <button className="btn" onClick={() => setImporting(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={submitImport}>Submit</button>
            </>
          }
        >
          {importMessage && (
            <div className="form-error" style={{ marginBottom: 12 }}>{importMessage}</div>
          )}
          {importErrors.length > 0 && (
            <div className="form-error" style={{ marginBottom: 12 }}>
              <strong>Row errors:</strong>
              <ul style={{ marginTop: 4, marginBottom: 0 }}>
                {importErrors.map((e, idx) => (
                  <li key={idx}>row {e.row_index}: {e.error_kind}{e.sku ? ` (${e.sku})` : ''}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="form-row">
            <div className="form-group">
              <label>Source Warehouse</label>
              <select
                className="form-select"
                value={importForm.source_warehouse_code}
                onChange={(e) => setImportForm({ ...importForm, source_warehouse_code: e.target.value })}
              >
                <option value="">Select source...</option>
                {warehouses.map((w) => (
                  <option key={w.warehouse_id} value={w.warehouse_code}>{w.warehouse_code}</option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label>Destination Warehouse</label>
              <select
                className="form-select"
                value={importForm.destination_warehouse_code}
                onChange={(e) => setImportForm({ ...importForm, destination_warehouse_code: e.target.value })}
              >
                <option value="">Select destination...</option>
                {warehouses.map((w) => (
                  <option key={w.warehouse_id} value={w.warehouse_code}>{w.warehouse_code}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="form-group">
            <label>Notes</label>
            <input
              className="form-input"
              value={importForm.notes}
              onChange={(e) => setImportForm({ ...importForm, notes: e.target.value })}
            />
          </div>
          <div className="form-group">
            <label>CSV File (columns: sku, quantity)</label>
            <input type="file" accept=".csv,text/csv" onChange={onImportFile} />
          </div>
          {importRows.length > 0 && (
            <div style={{ marginTop: 12, fontSize: 13 }}>
              <strong>{importRows.length} row(s) ready</strong>
              <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: 6 }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--border)' }}>
                    <th style={thStyle}>SKU</th>
                    <th style={{ ...thStyle, textAlign: 'right' }}>Quantity</th>
                  </tr>
                </thead>
                <tbody>
                  {importRows.slice(0, 50).map((r, idx) => (
                    <tr key={idx}>
                      <td className="mono" style={tdStyle}>{r.sku}</td>
                      <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{r.quantity}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {importRows.length > 50 && (
                <p style={{ color: 'var(--text-secondary)' }}>+{importRows.length - 50} more...</p>
              )}
            </div>
          )}
        </Modal>
      )}

      {shortageState && (
        <Modal
          title={`TO ${shortageState.to_number} created with shortages`}
          onClose={() => setShortageState(null)}
          footer={
            <>
              <button className="btn" onClick={() => downloadShortageCsv(shortageState.shortages)}>Download Shortage CSV</button>
              <button className="btn btn-danger" onClick={shortageCancelTO}>Cancel TO</button>
              <button className="btn btn-primary" onClick={shortageCreateWithAvailable}>Create with Available</button>
            </>
          }
        >
          <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
            The TO was created with committed quantities reduced to
            available stock at the source warehouse. Lines below show
            the shortfall.
          </p>
          <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                <th style={thStyle}>SKU</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Requested</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Available</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Committed</th>
                <th style={{ ...thStyle, textAlign: 'right' }}>Shortfall</th>
              </tr>
            </thead>
            <tbody>
              {shortageState.shortages.map((s, idx) => (
                <tr key={idx} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td className="mono" style={tdStyle}>{s.sku}</td>
                  <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{s.requested_qty}</td>
                  <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{s.available_qty}</td>
                  <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{s.committed_qty}</td>
                  <td className="mono" style={{ ...tdStyle, textAlign: 'right', color: 'var(--accent)' }}>{s.shortfall}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Modal>
      )}
    </div>
  );
}
