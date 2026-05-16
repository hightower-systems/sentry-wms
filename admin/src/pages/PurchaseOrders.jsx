import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

const STATUS_OPTIONS = ['All', 'OPEN', 'PARTIAL', 'RECEIVED', 'CLOSED'];

export default function PurchaseOrders() {
  const [searchParams] = useSearchParams();
  const [search, setSearch] = useState(searchParams.get('q') || '');
  const [orders, setOrders] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('All');
  const [selectedPO, setSelectedPO] = useState(null);
  const [poLines, setPOLines] = useState([]);
  const [editing, setEditing] = useState(null);
  const [editForm, setEditForm] = useState({});
  const [editError, setEditError] = useState('');
  const [confirmClose, setConfirmClose] = useState(false);

  useEffect(() => { loadOrders(); }, [page, statusFilter, search]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function loadOrders() {
    const qp = new URLSearchParams({ page: String(page), per_page: '50' });
    if (statusFilter !== 'All') qp.set('status', statusFilter);
    if (search) qp.set('q', search);
    const res = await api.get(`/admin/purchase-orders?${qp}`);
    if (res?.ok) {
      const data = await res.json();
      setOrders(data.purchase_orders || []);
      setPagination({ page: data.page, pages: data.pages, total: data.total });
    }
  }

  async function viewPO(po) {
    const res = await api.get(`/admin/purchase-orders/${po.po_id || po.id}`);
    if (res?.ok) {
      const data = await res.json();
      setSelectedPO(data.purchase_order);
      setPOLines(data.lines || []);
    }
  }

  function handleStatusChange(e) {
    setStatusFilter(e.target.value);
    setPage(1);
  }

  function handlePageChange(newPage) {
    setPage(newPage);
  }

  function openEdit(po) {
    setEditing(po);
    setEditForm({
      po_number: po.po_number || '',
      vendor_name: po.vendor_name || '',
      expected_date: po.expected_date ? po.expected_date.slice(0, 10) : '',
      notes: po.notes || '',
    });
    setEditError('');
  }

  async function saveEdit() {
    setEditError('');
    const body = {
      po_number: editForm.po_number,
      vendor_name: editForm.vendor_name || null,
      expected_date: editForm.expected_date || null,
      notes: editForm.notes || null,
    };
    const res = await api.put(`/admin/purchase-orders/${editing.po_id}`, body);
    if (res?.ok) {
      setEditing(null);
      loadOrders();
    } else {
      const data = await res?.json();
      setEditError(data?.error || 'Failed to save');
    }
  }

  async function closePO() {
    setEditError('');
    const res = await api.post(`/admin/purchase-orders/${editing.po_id}/close`, {});
    if (res?.ok) {
      setConfirmClose(false);
      setEditing(null);
      loadOrders();
    } else {
      const data = await res?.json();
      setEditError(data?.error || 'Failed to close');
      setConfirmClose(false);
    }
  }

  async function reopenPO() {
    setEditError('');
    const res = await api.post(`/admin/purchase-orders/${editing.po_id}/reopen`, {});
    if (res?.ok) {
      setEditing(null);
      loadOrders();
    } else {
      const data = await res?.json();
      setEditError(data?.error || 'Failed to reopen');
    }
  }

  // Variance = ordered - received per line. Columns match the
  // reconciliation report ops uses to chase short shipments with
  // the vendor; receiving has the same five columns in the same
  // order so an exported CSV slots directly into that workflow.
  function exportPOCsv() {
    if (!selectedPO || poLines.length === 0) return;
    const csvEscape = (v) => {
      if (v == null) return '';
      const s = String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const headers = ['SKU', 'Item Name', 'Ordered', 'Received', 'Variance'];
    const lines = [headers.join(',')];
    for (const l of poLines) {
      const ordered = l.quantity_ordered || 0;
      const received = l.quantity_received || 0;
      lines.push([
        csvEscape(l.sku),
        csvEscape(l.item_name),
        csvEscape(ordered),
        csvEscape(received),
        csvEscape(ordered - received),
      ].join(','));
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    const today = new Date().toISOString().split('T')[0];
    link.download = `po_${selectedPO.po_number}_${today}.csv`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  const columns = [
    { key: 'po_number', label: 'PO Number', mono: true },
    { key: 'vendor_name', label: 'Vendor' },
    { key: 'expected_date', label: 'Expected Date', mono: true, render: (r) => r.expected_date ? new Date(r.expected_date).toLocaleDateString() : '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'created_at', label: 'Created', render: (r) => r.created_at ? new Date(r.created_at).toLocaleDateString() : '-' },
    { key: 'actions', label: '', render: (r) => (
      <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); openEdit(r); }} aria-label="Edit" title="Edit">&#9998;</button>
    )},
  ];

  return (
    <div>
      <PageHeader title="Purchase Orders" />

      <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
        <label style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Status:</label>
        <select className="form-select" value={statusFilter} onChange={handleStatusChange} style={{ width: 160 }}>
          {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <input
          className="form-input"
          style={{ maxWidth: 320 }}
          placeholder="Search by PO number or vendor"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
        />
      </div>

      <DataTable
        columns={columns}
        data={orders}
        pagination={pagination}
        onPageChange={handlePageChange}
        onRowClick={viewPO}
        emptyMessage="No purchase orders found"
      />

      {selectedPO && (
        <Modal
          title={`PO ${selectedPO.po_number}`}
          onClose={() => { setSelectedPO(null); setPOLines([]); }}
          footer={
            <>
              <button className="btn" onClick={exportPOCsv} disabled={poLines.length === 0}>Export CSV</button>
              <button className="btn" onClick={() => { setSelectedPO(null); setPOLines([]); }}>Close</button>
            </>
          }
          size="wide"
        >
          <section className="section">
            <div className="section-title">PO Summary</div>
            <div className="detail-grid detail-grid-2col" style={{ marginBottom: 0 }}>
              <span className="detail-label">Vendor</span><span>{selectedPO.vendor || '-'}</span>
              <span className="detail-label">Status</span><span><StatusTag status={selectedPO.status} /></span>
              <span className="detail-label">Expected Date</span><span className="mono">{selectedPO.expected_date ? new Date(selectedPO.expected_date).toLocaleDateString() : '-'}</span>
              <span className="detail-label">Notes</span><span>{selectedPO.notes || '-'}</span>
            </div>
          </section>

          <section className="section" style={{ marginBottom: 0 }}>
            <div className="section-title">Line Items</div>
            {poLines.length > 0 ? (
              <table className="lines-table">
                <thead>
                  <tr>
                    <th>SKU</th>
                    <th>Item Name</th>
                    <th style={{ textAlign: 'right' }}>Ordered</th>
                    <th style={{ textAlign: 'right' }}>Received</th>
                    <th style={{ textAlign: 'right' }}>Remaining</th>
                  </tr>
                </thead>
                <tbody>
                  {poLines.map((l, i) => {
                    const remaining = (l.quantity_ordered || 0) - (l.quantity_received || 0);
                    return (
                      <tr key={i}>
                        <td className="mono">{l.sku}</td>
                        <td style={{ color: 'var(--text-secondary)' }}>{l.item_name}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_ordered}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_received}</td>
                        <td className="mono" style={{ textAlign: 'right', color: remaining > 0 ? 'var(--copper)' : 'var(--text-secondary)', fontWeight: remaining > 0 ? 600 : 400 }}>{remaining}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : (
              <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No line items</p>
            )}
          </section>
        </Modal>
      )}

      {editing && (
        <Modal
          title={`Edit PO ${editing.po_number}`}
          onClose={() => { setEditing(null); setConfirmClose(false); }}
          footer={
            <>
              {editing.status === 'CLOSED' ? (
                <button className="btn" onClick={reopenPO}>Reopen Purchase Order</button>
              ) : (
                <button className="btn btn-danger" onClick={() => setConfirmClose(true)}>Close Purchase Order</button>
              )}
              <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
              <button className="btn btn-primary" onClick={saveEdit} disabled={editing.status === 'CLOSED'}>Save</button>
            </>
          }
        >
          {editError && <div className="form-error" style={{ marginBottom: 12 }}>{editError}</div>}
          <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
            PO header fields only. Line items (items + quantities) are fixed after PO
            create and are read-only here. Editing is restricted to POs in OPEN status.
          </p>
          <div className="form-row">
            <div className="form-group">
              <label>PO Number</label>
              <input className="form-input" value={editForm.po_number} onChange={(e) => setEditForm({ ...editForm, po_number: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Vendor</label>
              <input className="form-input" value={editForm.vendor_name} onChange={(e) => setEditForm({ ...editForm, vendor_name: e.target.value })} />
            </div>
          </div>
          <div className="form-group">
            <label>Expected Date</label>
            <input className="form-input" type="date" value={editForm.expected_date} onChange={(e) => setEditForm({ ...editForm, expected_date: e.target.value })} />
          </div>
          <div className="form-group">
            <label>Notes</label>
            <textarea className="form-input" rows={3} value={editForm.notes} onChange={(e) => setEditForm({ ...editForm, notes: e.target.value })} />
          </div>
        </Modal>
      )}

      {confirmClose && editing && (
        <Modal
          title={`Close PO ${editing.po_number}?`}
          onClose={() => setConfirmClose(false)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmClose(false)}>Cancel</button>
              <button className="btn btn-danger" onClick={closePO}>Close Purchase Order</button>
            </>
          }
        >
          <p style={{ fontSize: 13 }}>
            Close this PO? It will no longer appear in active receiving lists. This
            can be reversed by reopening.
          </p>
        </Modal>
      )}
    </div>
  );
}
