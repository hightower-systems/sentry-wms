import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

const STATUS_OPTIONS = ['All', 'OPEN', 'ALLOCATED', 'PICKING', 'PICKED', 'PACKING', 'PACKED', 'SHIPPED', 'CANCELLED'];

export default function SalesOrders() {
  const [orders, setOrders] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('All');
  const [selectedSO, setSelectedSO] = useState(null);
  const [soLines, setSOLines] = useState([]);
  const [editing, setEditing] = useState(null);
  const [editForm, setEditForm] = useState({});
  const [editError, setEditError] = useState('');
  const [confirmCancel, setConfirmCancel] = useState(false);

  useEffect(() => { loadOrders(); }, [page, statusFilter]);

  async function loadOrders() {
    const params = `?page=${page}&per_page=50${statusFilter !== 'All' ? `&status=${statusFilter}` : ''}`;
    const res = await api.get(`/admin/sales-orders${params}`);
    if (res?.ok) {
      const data = await res.json();
      setOrders(data.sales_orders || []);
      setPagination({ page: data.page, pages: data.pages, total: data.total });
    }
  }

  async function viewSO(so) {
    const res = await api.get(`/admin/sales-orders/${so.so_id}`);
    if (res?.ok) {
      const data = await res.json();
      setSelectedSO(data.sales_order);
      setSOLines(data.lines || []);
    }
  }

  function openEdit(so) {
    setEditing(so);
    setEditForm({
      so_number: so.so_number || '',
      customer_name: so.customer_name || '',
      customer_phone: so.customer_phone || '',
      ship_address: so.ship_address || '',
      ship_method: so.ship_method || '',
      ship_by_date: so.ship_by_date ? so.ship_by_date.slice(0, 10) : '',
    });
    setEditError('');
  }

  async function saveEdit() {
    setEditError('');
    const body = {
      so_number: editForm.so_number,
      customer_name: editForm.customer_name || null,
      customer_phone: editForm.customer_phone || null,
      ship_address: editForm.ship_address || null,
      ship_method: editForm.ship_method || null,
      ship_by_date: editForm.ship_by_date || null,
    };
    const res = await api.put(`/admin/sales-orders/${editing.so_id}`, body);
    if (res?.ok) {
      setEditing(null);
      loadOrders();
    } else {
      const data = await res?.json();
      setEditError(data?.error || 'Failed to save');
    }
  }

  async function cancelSO() {
    setEditError('');
    const res = await api.post(`/admin/sales-orders/${editing.so_id}/cancel`, {});
    if (res?.ok) {
      setConfirmCancel(false);
      setEditing(null);
      loadOrders();
    } else {
      const data = await res?.json();
      setEditError(data?.error || 'Failed to cancel order');
      setConfirmCancel(false);
    }
  }

  const columns = [
    { key: 'so_number', label: 'SO Number', mono: true },
    { key: 'customer_name', label: 'Customer' },
    { key: 'ship_by_date', label: 'Ship By', mono: true, render: (r) => r.ship_by_date ? new Date(r.ship_by_date).toLocaleDateString() : '-' },
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
      <PageHeader title="Sales Orders" />

      <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
        <label style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Status:</label>
        <select className="form-select" value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }} style={{ width: 160 }}>
          {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      <DataTable
        columns={columns}
        data={orders}
        pagination={pagination}
        onPageChange={setPage}
        onRowClick={viewSO}
        emptyMessage="No sales orders found"
      />

      {selectedSO && (
        <Modal
          title={`SO ${selectedSO.so_number}`}
          onClose={() => { setSelectedSO(null); setSOLines([]); }}
          footer={<button className="btn" onClick={() => { setSelectedSO(null); setSOLines([]); }}>Close</button>}
        >
          <div className="detail-grid" style={{ marginBottom: 16 }}>
            <span className="detail-label">Customer</span><span>{selectedSO.customer_name || '-'}</span>
            <span className="detail-label">Status</span><span><StatusTag status={selectedSO.status} /></span>
            <span className="detail-label">Ship By</span><span className="mono">{selectedSO.ship_by_date ? new Date(selectedSO.ship_by_date).toLocaleDateString() : '-'}</span>
            <span className="detail-label">Ship Method</span><span>{selectedSO.ship_method || '-'}</span>
            <span className="detail-label">Ship Address</span><span>{selectedSO.ship_address || '-'}</span>
          </div>

          {soLines.length > 0 ? (
            <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)' }}>
                  <th style={thStyle}>SKU</th>
                  <th style={thStyle}>Item Name</th>
                  <th style={{ ...thStyle, textAlign: 'right' }}>Ordered</th>
                  <th style={{ ...thStyle, textAlign: 'right' }}>Picked</th>
                  <th style={{ ...thStyle, textAlign: 'right' }}>Shipped</th>
                </tr>
              </thead>
              <tbody>
                {soLines.map((l, i) => (
                  <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td className="mono" style={tdStyle}>{l.sku}</td>
                    <td style={{ ...tdStyle, color: 'var(--text-secondary)' }}>{l.item_name}</td>
                    <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.quantity_ordered}</td>
                    <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.quantity_picked}</td>
                    <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.quantity_shipped}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No line items</p>
          )}
        </Modal>
      )}

      {editing && (
        <Modal
          title={`Edit SO ${editing.so_number}`}
          onClose={() => { setEditing(null); setConfirmCancel(false); }}
          footer={
            <>
              {editing.status === 'OPEN' && (
                <button className="btn btn-danger" onClick={() => setConfirmCancel(true)}>Cancel Order</button>
              )}
              <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
              <button className="btn btn-primary" onClick={saveEdit} disabled={editing.status !== 'OPEN'}>Save</button>
            </>
          }
        >
          {editError && <div className="form-error" style={{ marginBottom: 12 }}>{editError}</div>}
          <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
            SO header fields only. Line items are fixed after SO create. Editing is
            restricted to orders in OPEN status; once picking has started, header
            fields are frozen to preserve the fulfillment record.
          </p>
          <div className="form-row">
            <div className="form-group">
              <label>SO Number</label>
              <input className="form-input" value={editForm.so_number} onChange={(e) => setEditForm({ ...editForm, so_number: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Customer</label>
              <input className="form-input" value={editForm.customer_name} onChange={(e) => setEditForm({ ...editForm, customer_name: e.target.value })} />
            </div>
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>Phone</label>
              <input className="form-input" value={editForm.customer_phone} onChange={(e) => setEditForm({ ...editForm, customer_phone: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Ship By</label>
              <input className="form-input" type="date" value={editForm.ship_by_date} onChange={(e) => setEditForm({ ...editForm, ship_by_date: e.target.value })} />
            </div>
          </div>
          <div className="form-group">
            <label>Ship Method</label>
            <input className="form-input" value={editForm.ship_method} onChange={(e) => setEditForm({ ...editForm, ship_method: e.target.value })} />
          </div>
          <div className="form-group">
            <label>Ship Address</label>
            <textarea className="form-input" rows={2} value={editForm.ship_address} onChange={(e) => setEditForm({ ...editForm, ship_address: e.target.value })} />
          </div>
        </Modal>
      )}

      {confirmCancel && editing && (
        <Modal
          title={`Cancel order ${editing.so_number}?`}
          onClose={() => setConfirmCancel(false)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmCancel(false)}>Keep Order</button>
              <button className="btn btn-danger" onClick={cancelSO}>Cancel Order</button>
            </>
          }
        >
          <p style={{ fontSize: 13 }}>
            Cancel this order? It will no longer appear in picking/shipping queues.
            This action cannot be undone from the UI.
          </p>
        </Modal>
      )}
    </div>
  );
}
