import { useState, useEffect, useRef } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import PageHeader from '../components/PageHeader.jsx';

export default function Adjustments() {
  const { warehouseId } = useWarehouse();
  // avid-overhaul-mk1 P9.2: bin + item lookups switched from
  // "load every row client-side and filter in JS" to debounced
  // server-side search. The old loadBins() defaulted to per_page=50
  // so a 3000-bin warehouse only showed the first batch; loadItems()
  // capped at 1000 which broke at 30K SKUs in production. Both now
  // hit the server's ILIKE :q index path on each keystroke.
  const [binResults, setBinResults] = useState([]);
  const [itemResults, setItemResults] = useState([]);
  const [adjustments, setAdjustments] = useState([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState('');
  const [error, setError] = useState('');

  const [binSearch, setBinSearch] = useState('');
  const [binOpen, setBinOpen] = useState(false);
  const [binSearching, setBinSearching] = useState(false);
  const [itemSearch, setItemSearch] = useState('');
  const [itemOpen, setItemOpen] = useState(false);
  const [itemSearching, setItemSearching] = useState(false);
  const binRef = useRef(null);
  const itemRef = useRef(null);

  const [form, setForm] = useState({
    bin_id: '',
    item_id: '',
    adjustment_type: 'Add',
    quantity: '',
    reason: '',
  });

  // Close dropdowns on outside click
  useEffect(() => {
    function handleClick(e) {
      if (binRef.current && !binRef.current.contains(e.target)) setBinOpen(false);
      if (itemRef.current && !itemRef.current.contains(e.target)) setItemOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  useEffect(() => {
    loadAdjustments();
    // Clear selections when the warehouse switches so the operator
    // does not accidentally apply an adjustment to a bin in a
    // warehouse they no longer have on screen.
    setForm((p) => ({ ...p, bin_id: '', item_id: '' }));
    setBinSearch('');
    setItemSearch('');
    setBinResults([]);
    setItemResults([]);
  }, [warehouseId]);  // eslint-disable-line react-hooks/exhaustive-deps

  // Debounced bin search. Triggers when the operator types in the
  // bin field; ignores empty / one-char queries to avoid round-tripping
  // on every focus. 200 ms matches the PO line typeahead.
  useEffect(() => {
    const q = binSearch.trim();
    if (!warehouseId || q.length < 1 || form.bin_id) {
      setBinResults([]);
      return;
    }
    setBinSearching(true);
    const handle = setTimeout(async () => {
      const res = await api.get(
        `/admin/bins?warehouse_id=${warehouseId}&q=${encodeURIComponent(q)}&per_page=25`,
      );
      setBinSearching(false);
      if (!res?.ok) return;
      const data = await res.json();
      setBinResults(data.bins || []);
    }, 200);
    return () => clearTimeout(handle);
  }, [binSearch, warehouseId, form.bin_id]);

  // Same shape for items. Three-char minimum keeps the 30K-SKU
  // catalog from returning huge result sets on a two-letter prefix.
  useEffect(() => {
    const q = itemSearch.trim();
    if (q.length < 2 || form.item_id) {
      setItemResults([]);
      return;
    }
    setItemSearching(true);
    const handle = setTimeout(async () => {
      const res = await api.get(
        `/admin/items?q=${encodeURIComponent(q)}&per_page=25&active=true`,
      );
      setItemSearching(false);
      if (!res?.ok) return;
      const data = await res.json();
      setItemResults(data.items || []);
    }, 200);
    return () => clearTimeout(handle);
  }, [itemSearch, form.item_id]);

  async function loadAdjustments() {
    setLoading(true);
    const res = await api.get(`/admin/adjustments/list?warehouse_id=${warehouseId}`);
    if (res?.ok) {
      const data = await res.json();
      setAdjustments(data.adjustments || []);
    }
    setLoading(false);
  }

  function updateForm(key, value) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function selectBin(bin) {
    updateForm('bin_id', bin.bin_id);
    setBinSearch(bin.bin_code);
    setBinOpen(false);
  }

  function selectItem(item) {
    updateForm('item_id', item.item_id);
    setItemSearch(`${item.sku}  -  ${item.item_name}`);
    setItemOpen(false);
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    setSuccess('');

    if (!form.bin_id || !form.item_id || !form.quantity) {
      setError('Bin, item, and quantity are required.');
      return;
    }

    const reason = (form.reason || '').trim();
    if (!reason) {
      setError('Reason is required for audit traceability.');
      return;
    }

    const qty = parseInt(form.quantity, 10);
    if (isNaN(qty) || qty <= 0) {
      setError('Quantity must be a positive number.');
      return;
    }

    setSubmitting(true);
    try {
      const res = await api.post('/admin/adjustments/direct', {
        warehouse_id: warehouseId,
        bin_id: form.bin_id,
        item_id: form.item_id,
        adjustment_type: form.adjustment_type.toLowerCase(),
        quantity: qty,
        reason,
      });

      if (res?.ok) {
        setSuccess('Adjustment created successfully.');
        setForm({ bin_id: '', item_id: '', adjustment_type: 'Add', quantity: '', reason: '' });
        setBinSearch('');
        setItemSearch('');
        loadAdjustments();
      } else {
        const data = await res.json().catch(() => null);
        setError(data?.error || data?.message || 'Failed to create adjustment.');
      }
    } catch {
      setError('Network error. Please try again.');
    }
    setSubmitting(false);
  }

  function typeTag(type) {
    if (!type) return '-';
    const t = type.toLowerCase();
    if (t === 'add') return <span className="tag tag-success">Add</span>;
    if (t === 'remove') return <span className="tag tag-danger">Remove</span>;
    return <span className="tag tag-warning">{type}</span>;
  }

  return (
    <div>
      <PageHeader title="Inventory Adjustments" />

      <div className="settings-section">
        <h3>Create Adjustment</h3>

        {success && <div className="alert alert-success" style={{ marginBottom: 12, padding: '8px 12px', background: '#d4edda', color: '#155724', borderRadius: 8 }}>{success}</div>}
        {error && <div className="alert alert-error" style={{ marginBottom: 12, padding: '8px 12px', background: '#f8d7da', color: '#842029', borderRadius: 8 }}>{error}</div>}

        <form onSubmit={handleSubmit}>
          <div className="form-row">
            <div className="form-group" ref={binRef} style={{ position: 'relative' }}>
              <label>Bin {binSearching && <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>(searching...)</span>}</label>
              <input
                className="form-input mono"
                placeholder="Type bin code to search"
                value={binSearch}
                onChange={(e) => { setBinSearch(e.target.value); updateForm('bin_id', ''); setBinOpen(true); }}
                onFocus={() => setBinOpen(true)}
                autoComplete="off"
              />
              {binOpen && binResults.length > 0 && (
                <div style={dropdownStyle}>
                  {binResults.map((b) => (
                    <div key={b.bin_id} style={dropdownItemStyle} onMouseDown={() => selectBin(b)}>
                      <span className="mono">{b.bin_code}</span> {b.bin_type ? `(${b.bin_type})` : ''}
                    </div>
                  ))}
                </div>
              )}
              {binOpen && !binSearching && binSearch.trim().length >= 1 && !form.bin_id && binResults.length === 0 && (
                <div style={{ ...dropdownStyle, padding: 12, fontSize: 12, color: 'var(--text-secondary)' }}>
                  No bins match "{binSearch.trim()}" in this warehouse.
                </div>
              )}
            </div>

            <div className="form-group" ref={itemRef} style={{ position: 'relative' }}>
              <label>Item {itemSearching && <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>(searching...)</span>}</label>
              <input
                className="form-input"
                placeholder="Type SKU or item name (min 2 chars)"
                value={itemSearch}
                onChange={(e) => { setItemSearch(e.target.value); updateForm('item_id', ''); setItemOpen(true); }}
                onFocus={() => setItemOpen(true)}
                autoComplete="off"
              />
              {itemOpen && itemResults.length > 0 && (
                <div style={dropdownStyle}>
                  {itemResults.map((i) => (
                    <div key={i.item_id} style={dropdownItemStyle} onMouseDown={() => selectItem(i)}>
                      <strong className="mono">{i.sku}</strong>  -  {i.item_name}
                    </div>
                  ))}
                </div>
              )}
              {itemOpen && !itemSearching && itemSearch.trim().length >= 2 && !form.item_id && itemResults.length === 0 && (
                <div style={{ ...dropdownStyle, padding: 12, fontSize: 12, color: 'var(--text-secondary)' }}>
                  No items match "{itemSearch.trim()}".
                </div>
              )}
            </div>
          </div>

          <div className="form-row">
            <div className="form-group">
              <label>Type</label>
              <div style={{ display: 'flex', gap: 16, paddingTop: 6 }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                  <input type="radio" name="adjustment_type" value="Add" checked={form.adjustment_type === 'Add'} onChange={() => updateForm('adjustment_type', 'Add')} />
                  Add
                </label>
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                  <input type="radio" name="adjustment_type" value="Remove" checked={form.adjustment_type === 'Remove'} onChange={() => updateForm('adjustment_type', 'Remove')} />
                  Remove
                </label>
              </div>
            </div>

            <div className="form-group">
              <label>Quantity</label>
              <input
                className="form-input"
                type="number"
                min="1"
                placeholder="Qty"
                value={form.quantity}
                onChange={(e) => updateForm('quantity', e.target.value)}
              />
            </div>

            <div className="form-group" style={{ flex: 2 }}>
              <label>Reason / Notes</label>
              <textarea
                className="form-input"
                rows={2}
                placeholder="Reason for adjustment..."
                value={form.reason}
                onChange={(e) => updateForm('reason', e.target.value)}
                style={{ resize: 'vertical' }}
              />
            </div>
          </div>

          <div style={{ marginTop: 12 }}>
            <button className="btn btn-primary" type="submit" disabled={submitting}>
              {submitting ? 'Submitting...' : 'Create Adjustment'}
            </button>
          </div>
        </form>
      </div>

      <div className="settings-section" style={{ marginTop: 24 }}>
        <h3>Recent Adjustments</h3>
        {loading ? (
          <p style={{ color: '#888' }}>Loading...</p>
        ) : adjustments.length === 0 ? (
          <p style={{ color: '#888' }}>No adjustments found.</p>
        ) : (
          <div className="data-table-wrapper">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Type</th>
                  <th>SKU</th>
                  <th>Item</th>
                  <th>Bin</th>
                  <th>Qty</th>
                  <th>Reason</th>
                  <th>User</th>
                </tr>
              </thead>
              <tbody>
                {adjustments.map((adj, idx) => {
                  const qc = typeof adj.quantity_change === 'number' ? adj.quantity_change : null;
                  const inferredType = qc === null ? null : qc >= 0 ? 'add' : 'remove';
                  return (
                    <tr key={adj.adjustment_id || idx}>
                      <td style={{ fontFamily: 'monospace', whiteSpace: 'nowrap' }}>{adj.adjusted_at ? new Date(adj.adjusted_at).toLocaleString() : '-'}</td>
                      <td>{typeTag(inferredType)}</td>
                      <td style={{ fontFamily: 'monospace' }}>{adj.sku || '-'}</td>
                      <td>{adj.item_name || '-'}</td>
                      <td style={{ fontFamily: 'monospace' }}>{adj.bin_code || '-'}</td>
                      <td style={{ fontWeight: 600 }}>{qc === null ? '-' : Math.abs(qc)}</td>
                      <td>{adj.reason_detail || adj.reason_code || '-'}</td>
                      <td>{adj.username || '-'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

const dropdownStyle = {
  position: 'absolute',
  top: '100%',
  left: 0,
  right: 0,
  maxHeight: 200,
  overflowY: 'auto',
  background: '#fff',
  border: '1px solid #ddd',
  borderRadius: 8,
  boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
  zIndex: 100,
};

const dropdownItemStyle = {
  padding: '8px 12px',
  cursor: 'pointer',
  borderBottom: '1px solid #f0f0f0',
  fontSize: 13,
};
