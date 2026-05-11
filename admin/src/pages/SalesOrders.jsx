import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

const STATUS_OPTIONS = ['All', 'OPEN', 'ALLOCATED', 'PICKING', 'PICKED', 'PACKING', 'PACKED', 'SHIPPED', 'CANCELLED'];

// v1.8.0 (#268) per-component billing/shipping address fields. Order
// must match the canonical column ordering for round-trip consistency.
const ADDRESS_FIELD_KEYS = [
  'billing_address_name', 'billing_address_line1', 'billing_address_line2',
  'billing_address_city', 'billing_address_state',
  'billing_address_postal_code', 'billing_address_country',
  'billing_address_phone',
  'shipping_address_name', 'shipping_address_line1', 'shipping_address_line2',
  'shipping_address_city', 'shipping_address_state',
  'shipping_address_postal_code', 'shipping_address_country',
  'shipping_address_phone',
];

const ADDRESS_FIELD_LABELS = {
  billing_address_name: 'Name',
  billing_address_line1: 'Line 1',
  billing_address_line2: 'Line 2',
  billing_address_city: 'City',
  billing_address_state: 'State / Region',
  billing_address_postal_code: 'Postal Code',
  billing_address_country: 'Country',
  billing_address_phone: 'Phone',
  shipping_address_name: 'Name',
  shipping_address_line1: 'Line 1',
  shipping_address_line2: 'Line 2',
  shipping_address_city: 'City',
  shipping_address_state: 'State / Region',
  shipping_address_postal_code: 'Postal Code',
  shipping_address_country: 'Country',
  shipping_address_phone: 'Phone',
};

function NullableValue({ value }) {
  if (value === null || value === undefined || value === '') {
    return <span style={{ color: 'var(--text-secondary)' }}>-</span>;
  }
  return <span>{value}</span>;
}

export default function SalesOrders() {
  const [searchParams] = useSearchParams();
  const [search, setSearch] = useState(searchParams.get('q') || '');
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
  // v1.8.0 (#268) address edit: separate modal that calls PATCH
  // /sales-orders/<so_id>/address. Backend status gate: ADMIN at any
  // status, non-admin only at OPEN. Addresses live on the canonical
  // header so customer-service edits can land post-PICKED for
  // shipping fixes.
  const [addressEditing, setAddressEditing] = useState(null);
  const [addressForm, setAddressForm] = useState({});
  const [addressError, setAddressError] = useState('');

  useEffect(() => { loadOrders(); }, [page, statusFilter, search]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function loadOrders() {
    const qp = new URLSearchParams({ page: String(page), per_page: '50' });
    if (statusFilter !== 'All') qp.set('status', statusFilter);
    if (search) qp.set('q', search);
    const res = await api.get(`/admin/sales-orders?${qp}`);
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
      memo: so.memo || '',
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
      memo: editForm.memo || null,
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

  function openAddressEdit(so) {
    setAddressEditing(so);
    const form = {};
    for (const key of ADDRESS_FIELD_KEYS) {
      form[key] = so[key] || '';
    }
    setAddressForm(form);
    setAddressError('');
  }

  async function saveAddressEdit() {
    setAddressError('');
    // Empty string clears the column to NULL on the backend; we send
    // every field that the operator could have edited so a deletion
    // is also persisted.
    const body = {};
    for (const key of ADDRESS_FIELD_KEYS) {
      body[key] = addressForm[key] || '';
    }
    const res = await api.patch(
      `/admin/sales-orders/${addressEditing.so_id}/address`,
      body,
    );
    if (res?.ok) {
      setAddressEditing(null);
      // Refresh detail modal to show the saved values.
      const refresh = await api.get(
        `/admin/sales-orders/${addressEditing.so_id}`,
      );
      if (refresh?.ok) {
        const data = await refresh.json();
        setSelectedSO(data.sales_order);
        setSOLines(data.lines || []);
      }
    } else {
      const data = await res?.json();
      setAddressError(data?.error || 'Failed to save addresses');
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
        <input
          className="form-input"
          style={{ maxWidth: 320 }}
          placeholder="Search by SO number or customer"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
        />
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
            {/* v1.8.0 (#282) per-order cost fields. order_total +
                customer_shipping_paid arrive as strings on the wire to
                preserve Decimal precision; render literal. */}
            <span className="detail-label">Order Total</span>
            <span className="mono"><NullableValue value={selectedSO.order_total} /></span>
            <span className="detail-label">Shipping Paid</span>
            <span className="mono"><NullableValue value={selectedSO.customer_shipping_paid} /></span>
          </div>

          {/* v1.9.0 #315: free-text operator-facing note. Only shown
              when populated; render with whiteSpace: pre-wrap so
              embedded newlines from the source ERP survive. */}
          {selectedSO.memo && (
            <div style={{
              marginBottom: 16, padding: 10,
              borderLeft: '3px solid #b87333', backgroundColor: '#fdf6ed',
              whiteSpace: 'pre-wrap',
            }}>
              <div style={{
                fontSize: 11, fontWeight: 700, color: '#b87333',
                letterSpacing: 0.4, marginBottom: 4,
              }}>NOTE</div>
              <div style={{ fontSize: 13, lineHeight: 1.4 }}>{selectedSO.memo}</div>
            </div>
          )}

          {/* v1.8.0 (#268) per-component billing + shipping addresses.
              Each side gets its own card so a half-populated address
              renders cleanly without column shifts. */}
          <div style={{
            display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16,
            marginBottom: 16,
          }}>
            <div>
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                alignItems: 'center', marginBottom: 8,
              }}>
                <strong style={{ fontSize: 13 }}>Billing Address</strong>
                <button
                  className="btn btn-sm"
                  onClick={() => openAddressEdit(selectedSO)}
                  title="Edit billing + shipping addresses"
                >Edit Addresses</button>
              </div>
              <div className="detail-grid">
                {ADDRESS_FIELD_KEYS.filter((k) => k.startsWith('billing_')).map((k) => (
                  <span key={k} style={{ display: 'contents' }}>
                    <span className="detail-label">{ADDRESS_FIELD_LABELS[k]}</span>
                    <span><NullableValue value={selectedSO[k]} /></span>
                  </span>
                ))}
              </div>
            </div>
            <div>
              <strong style={{
                fontSize: 13, marginBottom: 8, display: 'block',
              }}>Shipping Address</strong>
              <div className="detail-grid">
                {ADDRESS_FIELD_KEYS.filter((k) => k.startsWith('shipping_')).map((k) => (
                  <span key={k} style={{ display: 'contents' }}>
                    <span className="detail-label">{ADDRESS_FIELD_LABELS[k]}</span>
                    <span><NullableValue value={selectedSO[k]} /></span>
                  </span>
                ))}
              </div>
            </div>
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

      {addressEditing && (
        <Modal
          title={`Edit Addresses - SO ${addressEditing.so_number}`}
          onClose={() => setAddressEditing(null)}
          footer={
            <>
              <button className="btn" onClick={() => setAddressEditing(null)}>Cancel</button>
              <button className="btn btn-primary" onClick={saveAddressEdit}>Save Addresses</button>
            </>
          }
        >
          {addressError && <div className="form-error" style={{ marginBottom: 12 }}>{addressError}</div>}
          <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
            Address edits go through a dedicated endpoint with a status
            gate: ADMIN can edit at any status, non-admin only on OPEN
            orders. Empty fields are saved as cleared. Header fields
            (SO number, customer, ship method) are edited via the main
            Edit button and remain locked once picking starts.
          </p>
          <div style={{
            display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16,
          }}>
            <div>
              <strong style={{ display: 'block', marginBottom: 8 }}>Billing</strong>
              {ADDRESS_FIELD_KEYS.filter((k) => k.startsWith('billing_')).map((k) => (
                <div key={k} className="form-group">
                  <label>{ADDRESS_FIELD_LABELS[k]}</label>
                  <input
                    className="form-input"
                    value={addressForm[k]}
                    onChange={(e) => setAddressForm({ ...addressForm, [k]: e.target.value })}
                  />
                </div>
              ))}
            </div>
            <div>
              <strong style={{ display: 'block', marginBottom: 8 }}>Shipping</strong>
              {ADDRESS_FIELD_KEYS.filter((k) => k.startsWith('shipping_')).map((k) => (
                <div key={k} className="form-group">
                  <label>{ADDRESS_FIELD_LABELS[k]}</label>
                  <input
                    className="form-input"
                    value={addressForm[k]}
                    onChange={(e) => setAddressForm({ ...addressForm, [k]: e.target.value })}
                  />
                </div>
              ))}
            </div>
          </div>
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
          <div className="form-group">
            <label>Note (memo)</label>
            <textarea
              className="form-input" rows={3}
              placeholder="Customer notes, e.g. leave at back door, fragile, double-box"
              maxLength={4096}
              value={editForm.memo}
              onChange={(e) => setEditForm({ ...editForm, memo: e.target.value })}
            />
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
