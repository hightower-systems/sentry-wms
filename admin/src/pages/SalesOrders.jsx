import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import { useAuth } from '../auth.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

const STATUS_OPTIONS = ['All', 'OPEN', 'ALLOCATED', 'PICKING', 'PICKED', 'PACKING', 'PACKED', 'SHIPPED', 'CANCELLED'];
const EDITABLE_STATUS_OPTIONS = ['OPEN', 'PICKING', 'PICKED', 'PACKING', 'PACKED', 'SHIPPED', 'CANCELLED'];
// Lines are terminal once the SO has shipped or cancelled. Header edits
// remain ADMIN-only for SHIPPED (legacy ShipRush backfill); for CANCELLED
// the operator should reopen via the cancel-undo workflow, not edit.
const LINE_TERMINAL_STATUSES = new Set(['SHIPPED', 'CANCELLED']);

// Matches PurchaseOrders.formatApiError: surfaces field-level details
// from the @validate_body decorator instead of the bare "validation_error".
function formatApiError(data, fallback) {
  if (!data) return fallback;
  if (Array.isArray(data.details) && data.details.length > 0) {
    return data.details
      .map((d) => {
        const field = Array.isArray(d.loc) && d.loc.length ? d.loc.join('.') : 'request';
        return `${field}: ${d.msg}`;
      })
      .join('; ');
  }
  return data.error || fallback;
}

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
  const { user } = useAuth();
  const isAdmin = user?.role === 'ADMIN';
  const hasSOFullEdit = isAdmin || (user?.allowed_overrides || []).includes('so-full-edit');

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
  // avid-overhaul-mk1 P7 line CRUD inside the SO edit modal. State
  // mirrors PurchaseOrders.jsx: editLines is the working copy, optimistic
  // PATCH/POST/DELETE update it without refetching; lineErrors keep
  // the inline rejection visible per row.
  const [editLines, setEditLines] = useState([]);
  const [lineErrors, setLineErrors] = useState({});
  const [newLineSku, setNewLineSku] = useState('');
  const [newLineQty, setNewLineQty] = useState('');
  const [newLineError, setNewLineError] = useState('');
  const [addingLine, setAddingLine] = useState(false);
  const [skuSuggestions, setSkuSuggestions] = useState([]);
  const [resolvedItem, setResolvedItem] = useState(null);
  // Allocation-release confirm: shows what will happen before the
  // PATCH/DELETE leaves the browser so the operator can back out.
  // intent === 'shrink' carries a target qty; 'delete' just removes.
  const [releaseConfirm, setReleaseConfirm] = useState(null);
  // Canonical source_system tag list for the source_system dropdown.
  // Fetched once; ADMIN + so-full-edit-override roles can repoint.
  const [sourceSystems, setSourceSystems] = useState([]);

  useEffect(() => { loadOrders(); }, [page, statusFilter, search]);  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    api.get('/admin/source-systems', { silentPermissionDenied: true }).then(async (res) => {
      if (!res?.ok) return;
      const data = await res.json();
      setSourceSystems(data.source_systems || []);
    });
  }, []);

  // Debounced SKU typeahead matching the PO edit modal. Quiet when the
  // edit modal is closed; minimum 2 characters to avoid spamming the
  // items endpoint on every keystroke.
  useEffect(() => {
    if (!editing) return;
    const sku = newLineSku.trim();
    if (sku.length < 2) {
      setSkuSuggestions([]);
      setResolvedItem(null);
      return;
    }
    const handle = setTimeout(async () => {
      const res = await api.get(
        `/admin/items?q=${encodeURIComponent(sku)}&per_page=10&active=true`,
        { silentPermissionDenied: true },
      );
      if (!res?.ok) {
        setSkuSuggestions([]);
        setResolvedItem(null);
        return;
      }
      const data = await res.json();
      const items = data.items || [];
      setSkuSuggestions(items);
      const exact = items.find(
        (i) => String(i.sku || '').trim().toLowerCase() === sku.toLowerCase(),
      ) || null;
      setResolvedItem(exact);
    }, 200);
    return () => clearTimeout(handle);
  }, [newLineSku, editing]);

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

  async function openEdit(so) {
    // Fetch the full SO with its lines so the modal can drive line
    // CRUD without a second round-trip. Falls back to the row-level
    // values if the detail call fails so the operator at least sees
    // the basic edit form.
    const res = await api.get(`/admin/sales-orders/${so.so_id}`);
    let full = so;
    let lines = [];
    if (res?.ok) {
      const data = await res.json();
      full = data.sales_order || so;
      lines = data.lines || [];
    }
    setEditing(full);
    setEditForm({
      so_number: full.so_number || '',
      customer_name: full.customer_name || '',
      customer_phone: full.customer_phone || '',
      ship_address: full.ship_address || '',
      ship_method: full.ship_method || '',
      ship_by_date: full.ship_by_date ? full.ship_by_date.slice(0, 10) : '',
      memo: full.memo || '',
      status: full.status || 'OPEN',
      source_system: full.source_system || '',
    });
    setEditLines(lines);
    setLineErrors({});
    setNewLineSku('');
    setNewLineQty('');
    setNewLineError('');
    setReleaseConfirm(null);
    setEditError('');
  }

  function closeEdit() {
    setEditing(null);
    setEditLines([]);
    setLineErrors({});
    setNewLineSku('');
    setNewLineQty('');
    setNewLineError('');
    setReleaseConfirm(null);
    setEditError('');
    setConfirmCancel(false);
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
    // Status edits are only sent when the operator changed the value.
    // Sending the current status would still 200 but it noises the
    // audit log with a self-transition row.
    if (editForm.status && editForm.status !== editing.status) {
      body.status = editForm.status;
    }
    // source_system reassignment: ADMIN-or-override gated server-side.
    // Send only when it changed; "" tells the backend to clear to NULL.
    if (hasSOFullEdit && editForm.source_system !== (editing.source_system || '')) {
      body.source_system = editForm.source_system || '';
    }
    const res = await api.put(`/admin/sales-orders/${editing.so_id}`, body);
    if (res?.ok) {
      closeEdit();
      loadOrders();
    } else {
      let data = null;
      try { data = await res?.json(); } catch (_) { /* non-JSON body */ }
      setEditError(formatApiError(data, 'Failed to save'));
    }
  }

  // ── line CRUD ─────────────────────────────────────────────────────────────

  async function resolveSku(sku) {
    const key = String(sku || '').trim().toLowerCase();
    if (!key) return null;
    const res = await api.get(
      `/admin/items?q=${encodeURIComponent(sku)}&per_page=10&active=true`,
      { silentPermissionDenied: true },
    );
    if (!res?.ok) return null;
    const data = await res.json();
    return (data.items || []).find(
      (i) => String(i.sku || '').trim().toLowerCase() === key,
    ) || null;
  }

  async function addLine() {
    setNewLineError('');
    const sku = newLineSku.trim();
    const qty = parseInt(newLineQty, 10);
    if (!sku) { setNewLineError('Enter a SKU'); return; }
    if (isNaN(qty) || qty <= 0) { setNewLineError('Enter a positive quantity'); return; }
    setAddingLine(true);
    try {
      let item = resolvedItem;
      if (!item || String(item.sku).toLowerCase() !== sku.toLowerCase()) {
        item = await resolveSku(sku);
      }
      if (!item) { setNewLineError(`Unknown SKU: ${sku}`); return; }
      const res = await api.post(
        `/admin/sales-orders/${editing.so_id}/lines`,
        { item_id: item.item_id, quantity_ordered: qty },
      );
      if (res?.ok) {
        const created = await res.json();
        setEditLines((ls) => [
          ...ls,
          { ...created, item_name: item.item_name, upc: item.upc },
        ]);
        setNewLineSku('');
        setNewLineQty('');
        setResolvedItem(null);
        setSkuSuggestions([]);
      } else {
        let data = null;
        try { data = await res?.json(); } catch (_) { /* non-JSON body */ }
        setNewLineError(formatApiError(data, 'Failed to add line'));
      }
    } finally {
      setAddingLine(false);
    }
  }

  // Commits a quantity_ordered edit. If new qty crosses below
  // quantity_allocated, we route through the release-confirm modal so
  // the operator explicitly acknowledges that pick-batch allocations
  // will be unwound.
  function requestLineQtyEdit(line, newQty) {
    if (newQty < (line.quantity_allocated || 0) && (line.quantity_allocated || 0) > 0) {
      setReleaseConfirm({ intent: 'shrink', line, newQty });
      return;
    }
    return commitLineQtyEdit(line, newQty);
  }

  async function commitLineQtyEdit(line, newQty) {
    setLineErrors((e) => ({ ...e, [line.so_line_id]: '' }));
    const res = await api.patch(
      `/admin/sales-orders/${editing.so_id}/lines/${line.so_line_id}`,
      { quantity_ordered: newQty },
    );
    if (res?.ok) {
      const data = await res.json();
      setEditLines((ls) => ls.map((l) =>
        l.so_line_id === line.so_line_id
          ? {
              ...l,
              quantity_ordered: newQty,
              // The server zeroes quantity_allocated when it releases.
              quantity_allocated: data.released_allocation > 0 ? 0 : l.quantity_allocated,
            }
          : l,
      ));
    } else {
      let data = null;
      try { data = await res?.json(); } catch (_) { /* non-JSON body */ }
      setLineErrors((e) => ({
        ...e,
        [line.so_line_id]: formatApiError(data, 'Failed to update'),
      }));
    }
  }

  function requestRemoveLine(line) {
    if ((line.quantity_allocated || 0) > 0) {
      setReleaseConfirm({ intent: 'delete', line });
      return;
    }
    return commitRemoveLine(line);
  }

  async function commitRemoveLine(line) {
    setLineErrors((e) => ({ ...e, [line.so_line_id]: '' }));
    const res = await api.delete(
      `/admin/sales-orders/${editing.so_id}/lines/${line.so_line_id}`,
    );
    if (res?.ok) {
      setEditLines((ls) => ls.filter((l) => l.so_line_id !== line.so_line_id));
    } else {
      let data = null;
      try { data = await res?.json(); } catch (_) { /* non-JSON body */ }
      setLineErrors((e) => ({
        ...e,
        [line.so_line_id]: formatApiError(data, 'Failed to remove'),
      }));
    }
  }

  async function confirmReleaseAndProceed() {
    const c = releaseConfirm;
    setReleaseConfirm(null);
    if (!c) return;
    if (c.intent === 'shrink') await commitLineQtyEdit(c.line, c.newQty);
    else if (c.intent === 'delete') await commitRemoveLine(c.line);
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
          size="wide"
        >
          <section className="section">
            <div className="section-title">Order Summary</div>
            <div className="detail-grid detail-grid-2col" style={{ marginBottom: 0 }}>
              <span className="detail-label">Customer</span><span>{selectedSO.customer_name || '-'}</span>
              <span className="detail-label">Status</span><span><StatusTag status={selectedSO.status} /></span>
              <span className="detail-label">Ship By</span><span className="mono">{selectedSO.ship_by_date ? new Date(selectedSO.ship_by_date).toLocaleDateString() : '-'}</span>
              <span className="detail-label">Ship Method</span><span>{selectedSO.ship_method || '-'}</span>
              {/* v1.8.0 (#282) per-order cost fields. order_total +
                  customer_shipping_paid arrive as strings on the wire to
                  preserve Decimal precision; render literal. */}
              <span className="detail-label">Order Total</span>
              <span className="mono"><NullableValue value={selectedSO.order_total} /></span>
              <span className="detail-label">Shipping Paid</span>
              <span className="mono"><NullableValue value={selectedSO.customer_shipping_paid} /></span>
              <span className="detail-label">Ship Address</span>
              <span style={{ gridColumn: 'span 3' }}>{selectedSO.ship_address || '-'}</span>
            </div>
          </section>

          {/* v1.9.0 #315: free-text operator-facing note. Only shown
              when populated; render with whiteSpace: pre-wrap so
              embedded newlines from the source ERP survive. */}
          {selectedSO.memo && (
            <section className="section">
              <div style={{
                padding: 10,
                borderLeft: '3px solid #b87333', backgroundColor: '#fdf6ed',
                whiteSpace: 'pre-wrap',
              }}>
                <div style={{
                  fontSize: 11, fontWeight: 700, color: '#b87333',
                  letterSpacing: 0.4, marginBottom: 4,
                }}>NOTE</div>
                <div style={{ fontSize: 13, lineHeight: 1.4 }}>{selectedSO.memo}</div>
              </div>
            </section>
          )}

          {/* v1.8.0 (#268) per-component billing + shipping addresses.
              Each side gets its own card so a half-populated address
              renders cleanly without column shifts. */}
          <section className="section">
            <div style={{
              display: 'flex', justifyContent: 'space-between',
              alignItems: 'center', marginBottom: 12,
            }}>
              <div className="section-title" style={{ marginBottom: 0 }}>Addresses</div>
              <button
                className="btn btn-sm"
                onClick={() => openAddressEdit(selectedSO)}
                title="Edit billing + shipping addresses"
              >Edit Addresses</button>
            </div>
            <div style={{
              display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16,
            }}>
              <div className="card">
                <div className="card-title">Billing Address</div>
                <div className="detail-grid" style={{ marginBottom: 0 }}>
                  {ADDRESS_FIELD_KEYS.filter((k) => k.startsWith('billing_')).map((k) => (
                    <span key={k} style={{ display: 'contents' }}>
                      <span className="detail-label">{ADDRESS_FIELD_LABELS[k]}</span>
                      <span><NullableValue value={selectedSO[k]} /></span>
                    </span>
                  ))}
                </div>
              </div>
              <div className="card">
                <div className="card-title">Shipping Address</div>
                <div className="detail-grid" style={{ marginBottom: 0 }}>
                  {ADDRESS_FIELD_KEYS.filter((k) => k.startsWith('shipping_')).map((k) => (
                    <span key={k} style={{ display: 'contents' }}>
                      <span className="detail-label">{ADDRESS_FIELD_LABELS[k]}</span>
                      <span><NullableValue value={selectedSO[k]} /></span>
                    </span>
                  ))}
                </div>
              </div>
            </div>
          </section>

          <section className="section" style={{ marginBottom: 0 }}>
            <div className="section-title">Line Items</div>
            {soLines.length > 0 ? (
              <table className="lines-table">
                <thead>
                  <tr>
                    <th>SKU</th>
                    <th>Item Name</th>
                    <th style={{ textAlign: 'right' }}>Ordered</th>
                    <th style={{ textAlign: 'right' }}>Picked</th>
                    <th style={{ textAlign: 'right' }}>Shipped</th>
                  </tr>
                </thead>
                <tbody>
                  {soLines.map((l, i) => (
                    <tr key={i}>
                      <td className="mono">{l.sku}</td>
                      <td style={{ color: 'var(--text-secondary)' }}>{l.item_name}</td>
                      <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_ordered}</td>
                      <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_picked}</td>
                      <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_shipped}</td>
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
          size="wide"
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

      {editing && (() => {
        const status = editing.status;
        const headerEditable = isAdmin || hasSOFullEdit || status === 'OPEN';
        const lineEditable = headerEditable && !LINE_TERMINAL_STATUSES.has(status);
        const sourceSystemEditable = hasSOFullEdit;
        return (
          <Modal
            title={`Edit SO ${editing.so_number}`}
            onClose={closeEdit}
            size="wide"
            footer={
              <>
                {status === 'OPEN' && (
                  <button className="btn btn-danger" onClick={() => setConfirmCancel(true)}>Cancel Order</button>
                )}
                <button className="btn" onClick={closeEdit}>Cancel</button>
                <button className="btn btn-primary" onClick={saveEdit} disabled={!headerEditable}>Save</button>
              </>
            }
          >
            {editError && <div className="form-error" style={{ marginBottom: 12 }}>{editError}</div>}
            {!headerEditable && (
              <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
                Header edits are locked while the SO is {status}. Ask an
                admin to grant the so-full-edit override if a post-OPEN
                correction is needed.
              </p>
            )}
            {headerEditable && status !== 'OPEN' && (
              <p style={{ fontSize: 12, color: 'var(--copper)', marginBottom: 12 }}>
                Editing past OPEN ({status}). Line changes that shrink
                or remove allocated quantity will release pick-batch
                allocations and require a re-pick.
              </p>
            )}
            <div className="form-row">
              <div className="form-group">
                <label>SO Number</label>
                <input className="form-input" disabled={!headerEditable} value={editForm.so_number} onChange={(e) => setEditForm({ ...editForm, so_number: e.target.value })} />
              </div>
              <div className="form-group">
                <label>Customer</label>
                <input className="form-input" disabled={!headerEditable} value={editForm.customer_name} onChange={(e) => setEditForm({ ...editForm, customer_name: e.target.value })} />
              </div>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label>Phone</label>
                <input className="form-input" disabled={!headerEditable} value={editForm.customer_phone} onChange={(e) => setEditForm({ ...editForm, customer_phone: e.target.value })} />
              </div>
              <div className="form-group">
                <label>Ship By</label>
                <input className="form-input" type="date" disabled={!headerEditable} value={editForm.ship_by_date} onChange={(e) => setEditForm({ ...editForm, ship_by_date: e.target.value })} />
              </div>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label>Status</label>
                <select
                  className="form-select"
                  disabled={!headerEditable}
                  value={editForm.status}
                  onChange={(e) => setEditForm({ ...editForm, status: e.target.value })}
                >
                  {EDITABLE_STATUS_OPTIONS.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>
              <div className="form-group">
                <label>Source System</label>
                <select
                  className="form-select"
                  disabled={!sourceSystemEditable}
                  value={editForm.source_system || ''}
                  onChange={(e) => setEditForm({ ...editForm, source_system: e.target.value })}
                  title={sourceSystemEditable
                    ? 'Reassign the ERP source tag (audit-logged)'
                    : 'ADMIN or so-full-edit override required to reassign'}
                >
                  <option value="">(none)</option>
                  {sourceSystems.map((s) => (
                    <option key={s.source_system} value={s.source_system}>
                      {s.source_system} {s.kind ? `(${s.kind})` : ''}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="form-group">
              <label>Ship Method</label>
              <input className="form-input" disabled={!headerEditable} value={editForm.ship_method} onChange={(e) => setEditForm({ ...editForm, ship_method: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Ship Address</label>
              <textarea className="form-input" rows={2} disabled={!headerEditable} value={editForm.ship_address} onChange={(e) => setEditForm({ ...editForm, ship_address: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Note (memo)</label>
              <textarea
                className="form-input" rows={3}
                placeholder="Customer notes, e.g. leave at back door, fragile, double-box"
                maxLength={4096}
                disabled={!headerEditable}
                value={editForm.memo}
                onChange={(e) => setEditForm({ ...editForm, memo: e.target.value })}
              />
            </div>

            <section className="section" style={{ marginTop: 16 }}>
              <div className="section-title">Line Items</div>
              {editLines.length > 0 ? (
                <table className="lines-table">
                  <thead>
                    <tr>
                      <th>SKU</th>
                      <th>Item Name</th>
                      <th style={{ textAlign: 'right' }}>Ordered</th>
                      <th style={{ textAlign: 'right' }}>Allocated</th>
                      <th style={{ textAlign: 'right' }}>Picked</th>
                      <th style={{ textAlign: 'right' }}>Shipped</th>
                      <th style={{ width: 40 }}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {editLines.map((l) => {
                      const removable = lineEditable
                        && (l.quantity_picked || 0) === 0
                        && (l.quantity_packed || 0) === 0
                        && (l.quantity_shipped || 0) === 0;
                      return (
                        <tr key={l.so_line_id}>
                          <td className="mono">{l.sku}</td>
                          <td style={{ color: 'var(--text-secondary)' }}>{l.item_name}</td>
                          <td style={{ textAlign: 'right' }}>
                            <LineQtyInput
                              line={l}
                              disabled={!lineEditable}
                              onCommit={requestLineQtyEdit}
                            />
                            {lineErrors[l.so_line_id] && (
                              <div className="form-error" style={{ fontSize: 11, marginTop: 4, textAlign: 'right' }}>
                                {lineErrors[l.so_line_id]}
                              </div>
                            )}
                          </td>
                          <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_allocated || 0}</td>
                          <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_picked || 0}</td>
                          <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_shipped || 0}</td>
                          <td style={{ textAlign: 'right' }}>
                            <button
                              className="btn btn-sm btn-danger"
                              onClick={() => requestRemoveLine(l)}
                              disabled={!removable}
                              title={!removable
                                ? (LINE_TERMINAL_STATUSES.has(status)
                                    ? `Lines locked while SO is ${status}`
                                    : 'Line has picked/packed/shipped units; unwind first')
                                : 'Remove line'}
                              aria-label="Remove line"
                            >&#10005;</button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No line items yet.</p>
              )}

              {lineEditable && (
                <div style={{
                  marginTop: 12, padding: 12,
                  background: 'var(--surface)',
                  borderRadius: 'var(--radius)',
                }}>
                  <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                    <div style={{ flex: '0 0 240px' }}>
                      <label style={{ fontSize: 11, color: 'var(--text-secondary)', fontWeight: 500 }}>SKU</label>
                      <input
                        className="form-input mono"
                        placeholder="Type SKU to search"
                        list="so-edit-sku-suggestions"
                        value={newLineSku}
                        onChange={(e) => setNewLineSku(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') addLine(); }}
                      />
                      <datalist id="so-edit-sku-suggestions">
                        {skuSuggestions.map((it) => (
                          <option key={it.item_id} value={it.sku}>{it.item_name}</option>
                        ))}
                      </datalist>
                    </div>
                    <div style={{ flex: '0 0 120px' }}>
                      <label style={{ fontSize: 11, color: 'var(--text-secondary)', fontWeight: 500 }}>Quantity</label>
                      <input
                        className="form-input"
                        type="number"
                        min={1}
                        placeholder="0"
                        value={newLineQty}
                        onChange={(e) => setNewLineQty(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') addLine(); }}
                      />
                    </div>
                    <div style={{ flexGrow: 1 }} />
                    <button
                      className="btn btn-primary"
                      onClick={addLine}
                      disabled={addingLine}
                      style={{ marginTop: 16 }}
                    >
                      {addingLine ? 'Adding...' : 'Add Line'}
                    </button>
                  </div>
                  {newLineError && (
                    <div className="form-error" style={{ marginTop: 8, fontSize: 12 }}>
                      {newLineError}
                    </div>
                  )}
                  {!newLineError && resolvedItem && (
                    <div style={{ marginTop: 8, fontSize: 12, color: 'var(--success)' }}>
                      Found: <strong>{resolvedItem.sku}</strong> - {resolvedItem.item_name}
                    </div>
                  )}
                  {!newLineError && !resolvedItem && newLineSku.trim().length >= 2 && skuSuggestions.length === 0 && (
                    <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-secondary)' }}>
                      No matches for "{newLineSku.trim()}". Click Add Line to retry the lookup.
                    </div>
                  )}
                </div>
              )}
            </section>
          </Modal>
        );
      })()}

      {releaseConfirm && (
        <Modal
          title="Release pick-batch allocation?"
          onClose={() => setReleaseConfirm(null)}
          footer={
            <>
              <button className="btn" onClick={() => setReleaseConfirm(null)}>Back</button>
              <button className="btn btn-danger" onClick={confirmReleaseAndProceed}>
                {releaseConfirm.intent === 'delete' ? 'Release + Remove Line' : 'Release + Update'}
              </button>
            </>
          }
        >
          <p style={{ fontSize: 13, marginBottom: 8 }}>
            Line <strong>{releaseConfirm.line.sku}</strong> currently has{' '}
            <strong>{releaseConfirm.line.quantity_allocated}</strong> units allocated to a pick batch.
          </p>
          <p style={{ fontSize: 13, marginBottom: 8 }}>
            {releaseConfirm.intent === 'delete'
              ? 'Removing this line will release those units back to the source bins. The line will then be deleted.'
              : `Reducing quantity to ${releaseConfirm.newQty} (below the allocated ${releaseConfirm.line.quantity_allocated}) will release the full line allocation back to the source bins.`}
          </p>
          <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
            Released inventory becomes available for re-allocation on the next pick-batch create. The action is audit-logged.
          </p>
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

// Matches PurchaseOrders.LineQtyInput - inline qty input with on-blur
// commit so an Enter or click-away triggers the PATCH. Re-syncs to
// the parent's value after a successful update so a server-side rewrite
// (e.g. allocation release zeroing the line) reflects immediately.
function LineQtyInput({ line, disabled, onCommit }) {
  const [val, setVal] = useState(String(line.quantity_ordered));
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setVal(String(line.quantity_ordered));
  }, [line.quantity_ordered]);

  async function commit() {
    const n = parseInt(val, 10);
    if (isNaN(n) || n <= 0 || n === line.quantity_ordered) {
      setVal(String(line.quantity_ordered));
      return;
    }
    setSaving(true);
    await onCommit(line, n);
    setSaving(false);
  }

  return (
    <input
      type="number"
      min={1}
      className="form-input mono"
      style={{ width: 88, textAlign: 'right', padding: '4px 8px' }}
      value={val}
      disabled={disabled || saving}
      onChange={(e) => setVal(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur(); }}
    />
  );
}
