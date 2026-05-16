import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

const STATUS_OPTIONS = ['All', 'OPEN', 'PARTIAL', 'RECEIVED', 'CLOSED', 'ARCHIVED'];
const ALL_PO_STATUSES = ['OPEN', 'PARTIAL', 'RECEIVED', 'CLOSED', 'ARCHIVED'];
// ARCHIVED is reachable only from CLOSED in the backend state
// machine. Surface that rule in the dropdown so an operator never
// sees a 400 from the server for picking an invalid transition.
const EDIT_LOCKED_STATUSES = new Set(['CLOSED', 'ARCHIVED']);

function statusDropdownOptions(currentStatus) {
  return ALL_PO_STATUSES.filter((s) => {
    if (s === currentStatus) return true;
    if (s === 'ARCHIVED') return currentStatus === 'CLOSED';
    return true;
  });
}

export default function PurchaseOrders() {
  const [searchParams] = useSearchParams();
  const [search, setSearch] = useState(searchParams.get('q') || '');
  const [orders, setOrders] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('All');
  // ARCHIVED POs are hidden by default to keep the active list clean.
  // The Show Archived toggle flips include_archived on the backend
  // call so an operator can audit completed orders when needed.
  const [showArchived, setShowArchived] = useState(false);
  const [selectedPO, setSelectedPO] = useState(null);
  const [poLines, setPOLines] = useState([]);
  // Edit modal state: full PO + its lines so the modal can drive the
  // line CRUD endpoints without a second fetch.
  const [editing, setEditing] = useState(null);
  const [editForm, setEditForm] = useState({});
  const [editError, setEditError] = useState('');
  const [editLines, setEditLines] = useState([]);
  const [lineErrors, setLineErrors] = useState({});
  const [newLineSku, setNewLineSku] = useState('');
  const [newLineQty, setNewLineQty] = useState('');
  const [newLineError, setNewLineError] = useState('');
  const [addingLine, setAddingLine] = useState(false);
  // Typeahead state: debounced fetch as the operator types in the
  // SKU field populates a datalist + drives a live "Found: ..."
  // preview so the operator can see the resolution before clicking
  // Add. Beats the original "type a SKU and pray it matches" UX.
  const [skuSuggestions, setSkuSuggestions] = useState([]);
  const [resolvedItem, setResolvedItem] = useState(null);

  useEffect(() => { loadOrders(); }, [page, statusFilter, search, showArchived]);  // eslint-disable-line react-hooks/exhaustive-deps

  // Debounced SKU lookup. Fires while the operator is typing in the
  // Add Line SKU input so suggestions surface without a server query
  // per keystroke. Skips when the modal is not open or the field
  // is empty.
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
    if (showArchived) qp.set('include_archived', 'true');
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

  async function openEdit(po) {
    // Load the full PO with its lines so the Edit modal can show
    // editable line items. Open detail-style endpoint reuses what
    // the read-only view modal also uses.
    const res = await api.get(`/admin/purchase-orders/${po.po_id || po.id}`);
    if (!res?.ok) return;
    const data = await res.json();
    const full = data.purchase_order;
    setEditing(full);
    setEditForm({
      po_number: full.po_number || '',
      vendor_name: full.vendor_name || '',
      expected_date: full.expected_date ? full.expected_date.slice(0, 10) : '',
      notes: full.notes || '',
      status: full.status,
    });
    setEditLines(data.lines || []);
    setLineErrors({});
    setNewLineSku('');
    setNewLineQty('');
    setNewLineError('');
    setEditError('');
  }

  function closeEdit() {
    setEditing(null);
    setEditLines([]);
    setLineErrors({});
    setNewLineSku('');
    setNewLineQty('');
    setNewLineError('');
    setEditError('');
  }

  async function saveEdit() {
    setEditError('');
    const body = {};
    // Only send header fields when the PO is still in an editable
    // status; for CLOSED / ARCHIVED only the status itself is allowed.
    const headerEditable = !EDIT_LOCKED_STATUSES.has(editing.status);
    if (headerEditable) {
      body.po_number = editForm.po_number;
      body.vendor_name = editForm.vendor_name || null;
      body.expected_date = editForm.expected_date || null;
      body.notes = editForm.notes || null;
    }
    if (editForm.status && editForm.status !== editing.status) {
      body.status = editForm.status;
    }
    if (Object.keys(body).length === 0) {
      closeEdit();
      return;
    }
    const res = await api.put(`/admin/purchase-orders/${editing.po_id}`, body);
    if (res?.ok) {
      closeEdit();
      loadOrders();
    } else {
      const data = await res?.json();
      setEditError(data?.error || 'Failed to save');
    }
  }

  async function updateLineQty(line, newQty) {
    setLineErrors((e) => ({ ...e, [line.po_line_id]: '' }));
    const res = await api.patch(
      `/admin/purchase-orders/${editing.po_id}/lines/${line.po_line_id}`,
      { quantity_ordered: newQty },
    );
    if (res?.ok) {
      setEditLines((ls) => ls.map((l) =>
        l.po_line_id === line.po_line_id
          ? { ...l, quantity_ordered: newQty }
          : l,
      ));
    } else {
      const data = await res?.json();
      setLineErrors((e) => ({
        ...e,
        [line.po_line_id]: data?.error || 'Failed to update',
      }));
    }
  }

  async function removeLine(line) {
    setLineErrors((e) => ({ ...e, [line.po_line_id]: '' }));
    const res = await api.delete(
      `/admin/purchase-orders/${editing.po_id}/lines/${line.po_line_id}`,
    );
    if (res?.ok) {
      setEditLines((ls) => ls.filter((l) => l.po_line_id !== line.po_line_id));
    } else {
      const data = await res?.json();
      setLineErrors((e) => ({
        ...e,
        [line.po_line_id]: data?.error || 'Failed to remove',
      }));
    }
  }

  // Resolves a user-typed SKU to an item_id via a narrow server query
  // and finds the exact case-insensitive match. Same pattern as
  // Settings.jsx Create PO so behavior is consistent across surfaces.
  async function resolveSku(sku) {
    const key = String(sku || '').trim().toLowerCase();
    if (!key) return null;
    const res = await api.get(
      `/admin/items?q=${encodeURIComponent(sku)}&per_page=10&active=true`,
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
    if (!sku) {
      setNewLineError('Enter a SKU');
      return;
    }
    if (isNaN(qty) || qty <= 0) {
      setNewLineError('Enter a positive quantity');
      return;
    }
    setAddingLine(true);
    try {
      // Prefer the resolvedItem from the live typeahead so we do not
      // pay a second round-trip when the operator already saw the
      // "Found" preview. Fall back to an on-demand server lookup so
      // the button still works if the operator types and clicks Add
      // faster than the 200ms debounce.
      let item = resolvedItem;
      if (!item || String(item.sku).toLowerCase() !== sku.toLowerCase()) {
        item = await resolveSku(sku);
      }
      if (!item) {
        setNewLineError(`Unknown SKU: ${sku}`);
        return;
      }
      const res = await api.post(
        `/admin/purchase-orders/${editing.po_id}/lines`,
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
        const data = await res?.json();
        setNewLineError(data?.error || 'Failed to add line');
      }
    } finally {
      setAddingLine(false);
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
        <label style={{
          display: 'flex', alignItems: 'center', gap: 6,
          fontSize: 13, color: 'var(--text-secondary)', cursor: 'pointer',
        }}>
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => { setShowArchived(e.target.checked); setPage(1); }}
          />
          Show Archived
        </label>
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
              <span className="detail-label">Vendor</span><span>{selectedPO.vendor_name || '-'}</span>
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
          onClose={closeEdit}
          footer={
            <>
              <button className="btn" onClick={closeEdit}>Cancel</button>
              <button className="btn btn-primary" onClick={saveEdit}>Save</button>
            </>
          }
          size="wide"
        >
          {editError && <div className="form-error" style={{ marginBottom: 12 }}>{editError}</div>}

          <section className="section">
            <div className="section-title">Header</div>
            <div className="form-row">
              <div className="form-group">
                <label>PO Number</label>
                <input
                  className="form-input"
                  value={editForm.po_number}
                  disabled={EDIT_LOCKED_STATUSES.has(editing.status)}
                  onChange={(e) => setEditForm({ ...editForm, po_number: e.target.value })}
                />
              </div>
              <div className="form-group">
                <label>Vendor</label>
                <input
                  className="form-input"
                  value={editForm.vendor_name}
                  disabled={EDIT_LOCKED_STATUSES.has(editing.status)}
                  onChange={(e) => setEditForm({ ...editForm, vendor_name: e.target.value })}
                />
              </div>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label>Expected Date</label>
                <input
                  className="form-input"
                  type="date"
                  value={editForm.expected_date}
                  disabled={EDIT_LOCKED_STATUSES.has(editing.status)}
                  onChange={(e) => setEditForm({ ...editForm, expected_date: e.target.value })}
                />
              </div>
              <div className="form-group">
                <label>Status</label>
                <select
                  className="form-select"
                  value={editForm.status}
                  onChange={(e) => setEditForm({ ...editForm, status: e.target.value })}
                >
                  {statusDropdownOptions(editing.status).map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>
            </div>
            <div className="form-group">
              <label>Notes</label>
              <textarea
                className="form-input"
                rows={2}
                value={editForm.notes}
                disabled={EDIT_LOCKED_STATUSES.has(editing.status)}
                onChange={(e) => setEditForm({ ...editForm, notes: e.target.value })}
              />
            </div>
            {EDIT_LOCKED_STATUSES.has(editing.status) && (
              <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                Header + line edits are locked while the PO is {editing.status}.
                Move the status off {editing.status} via the dropdown to resume editing.
              </p>
            )}
          </section>

          <section className="section">
            <div className="section-title">Line Items</div>
            {editLines.length > 0 ? (
              <table className="lines-table">
                <thead>
                  <tr>
                    <th>SKU</th>
                    <th>Item Name</th>
                    <th style={{ textAlign: 'right' }}>Ordered</th>
                    <th style={{ textAlign: 'right' }}>Received</th>
                    <th style={{ textAlign: 'right' }}>Variance</th>
                    <th style={{ width: 40 }}></th>
                  </tr>
                </thead>
                <tbody>
                  {editLines.map((l) => {
                    const variance = (l.quantity_ordered || 0) - (l.quantity_received || 0);
                    const lineLocked = EDIT_LOCKED_STATUSES.has(editing.status);
                    const removable = !lineLocked && (l.quantity_received || 0) === 0;
                    return (
                      <tr key={l.po_line_id}>
                        <td className="mono">{l.sku}</td>
                        <td style={{ color: 'var(--text-secondary)' }}>{l.item_name}</td>
                        <td style={{ textAlign: 'right' }}>
                          <LineQtyInput
                            line={l}
                            disabled={lineLocked}
                            onCommit={updateLineQty}
                          />
                          {lineErrors[l.po_line_id] && (
                            <div className="form-error" style={{ fontSize: 11, marginTop: 4, textAlign: 'right' }}>
                              {lineErrors[l.po_line_id]}
                            </div>
                          )}
                        </td>
                        <td className="mono" style={{ textAlign: 'right' }}>{l.quantity_received || 0}</td>
                        <td className="mono" style={{ textAlign: 'right', color: variance > 0 ? 'var(--copper)' : 'var(--text-secondary)', fontWeight: variance > 0 ? 600 : 400 }}>
                          {variance}
                        </td>
                        <td style={{ textAlign: 'right' }}>
                          <button
                            className="btn btn-sm btn-danger"
                            onClick={() => removeLine(l)}
                            disabled={!removable}
                            title={!removable
                              ? (lineLocked ? 'Locked while PO is ' + editing.status : 'Line has received units; reverse receipts first')
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

            {!EDIT_LOCKED_STATUSES.has(editing.status) && (
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
                      list="po-edit-sku-suggestions"
                      value={newLineSku}
                      onChange={(e) => setNewLineSku(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') addLine(); }}
                    />
                    <datalist id="po-edit-sku-suggestions">
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
                {/* Live resolution preview / error sit immediately under
                    the inputs so feedback is impossible to miss. */}
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
      )}
    </div>
  );
}

function LineQtyInput({ line, disabled, onCommit }) {
  const [val, setVal] = useState(String(line.quantity_ordered));
  const [saving, setSaving] = useState(false);

  // Sync local state when the parent updates after a successful PATCH
  // or after a refresh-from-server.
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
