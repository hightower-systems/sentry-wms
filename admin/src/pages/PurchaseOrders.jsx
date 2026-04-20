import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

const STATUS_OPTIONS = ['All', 'OPEN', 'PARTIAL', 'RECEIVED', 'CLOSED'];

export default function PurchaseOrders() {
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

  useEffect(() => { loadOrders(); }, [page, statusFilter]);

  async function loadOrders() {
    const params = `?page=${page}&per_page=50${statusFilter !== 'All' ? `&status=${statusFilter}` : ''}`;
    const res = await api.get(`/admin/purchase-orders${params}`);
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

  const thStyle = { textAlign: 'left', padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 };
  const tdStyle = { padding: '6px 8px' };

  return (
    <div>
      <PageHeader title="Purchase Orders" />

      <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
        <label style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Status:</label>
        <select className="form-select" value={statusFilter} onChange={handleStatusChange} style={{ width: 160 }}>
          {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
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
          footer={<button className="btn" onClick={() => { setSelectedPO(null); setPOLines([]); }}>Close</button>}
        >
          <div className="detail-grid" style={{ marginBottom: 16 }}>
            <span className="detail-label">Vendor</span><span>{selectedPO.vendor || '-'}</span>
            <span className="detail-label">Status</span><span><StatusTag status={selectedPO.status} /></span>
            <span className="detail-label">Expected Date</span><span className="mono">{selectedPO.expected_date ? new Date(selectedPO.expected_date).toLocaleDateString() : '-'}</span>
          </div>

          {poLines.length > 0 ? (
            <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)' }}>
                  <th style={thStyle}>SKU</th>
                  <th style={thStyle}>Item Name</th>
                  <th style={{ ...thStyle, textAlign: 'right' }}>Ordered</th>
                  <th style={{ ...thStyle, textAlign: 'right' }}>Received</th>
                  <th style={{ ...thStyle, textAlign: 'right' }}>Remaining</th>
                </tr>
              </thead>
              <tbody>
                {poLines.map((l, i) => {
                  const remaining = (l.quantity_ordered || 0) - (l.quantity_received || 0);
                  return (
                    <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td className="mono" style={tdStyle}>{l.sku}</td>
                      <td style={{ ...tdStyle, color: 'var(--text-secondary)' }}>{l.item_name}</td>
                      <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.quantity_ordered}</td>
                      <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.quantity_received}</td>
                      <td className="mono" style={{ ...tdStyle, textAlign: 'right', color: remaining > 0 ? 'var(--copper)' : 'var(--text-secondary)', fontWeight: remaining > 0 ? 600 : 400 }}>{remaining}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No line items</p>
          )}
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
