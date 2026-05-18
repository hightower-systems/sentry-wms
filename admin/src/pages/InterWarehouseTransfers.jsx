import { useState, useEffect, useRef } from 'react';
import { api } from '../api.js';
import PageHeader from '../components/PageHeader.jsx';

// avid-overhaul-mk1 P9.3: bin + item lookups switched from preloaded
// dropdowns to debounced server-side search so 30K SKUs and 3K bins
// stop hiding past the per_page cutoff. Same pattern as Adjustments
// (P9.2). The two bin pickers (source + dest) operate independently
// so a transfer between two large warehouses no longer blocks on a
// 50-row default.

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

function useDebouncedBinSearch(warehouseId, query, selectedId) {
  const [results, setResults] = useState([]);
  const [searching, setSearching] = useState(false);
  useEffect(() => {
    const q = (query || '').trim();
    if (!warehouseId || q.length < 1 || selectedId) {
      setResults([]);
      return;
    }
    setSearching(true);
    const handle = setTimeout(async () => {
      const res = await api.get(
        `/admin/bins?warehouse_id=${warehouseId}&q=${encodeURIComponent(q)}&per_page=25`,
      );
      setSearching(false);
      if (!res?.ok) return;
      const data = await res.json();
      setResults(data.bins || []);
    }, 200);
    return () => clearTimeout(handle);
  }, [warehouseId, query, selectedId]);
  return { results, searching };
}

export default function InterWarehouseTransfers() {
  const [warehouses, setWarehouses] = useState([]);
  const [transfers, setTransfers] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  const [form, setForm] = useState({
    source_warehouse_id: '',
    source_bin_id: '',
    destination_warehouse_id: '',
    destination_bin_id: '',
    item_id: '',
    quantity: '',
  });

  // Typeahead state per lookup field.
  const [sourceBinSearch, setSourceBinSearch] = useState('');
  const [sourceBinOpen, setSourceBinOpen] = useState(false);
  const [destBinSearch, setDestBinSearch] = useState('');
  const [destBinOpen, setDestBinOpen] = useState(false);
  const [itemSearch, setItemSearch] = useState('');
  const [itemOpen, setItemOpen] = useState(false);
  const [itemResults, setItemResults] = useState([]);
  const [itemSearching, setItemSearching] = useState(false);
  const sourceBinRef = useRef(null);
  const destBinRef = useRef(null);
  const itemRef = useRef(null);

  const sourceBinQuery = useDebouncedBinSearch(
    form.source_warehouse_id, sourceBinSearch, form.source_bin_id,
  );
  const destBinQuery = useDebouncedBinSearch(
    form.destination_warehouse_id, destBinSearch, form.destination_bin_id,
  );

  // Item search is warehouse-agnostic at the catalog level - the
  // transfer endpoint validates that the chosen item actually has
  // inventory in the source bin, so an unrelated catalog match just
  // bounces back with a clear error.
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

  useEffect(() => {
    function handleClick(e) {
      if (sourceBinRef.current && !sourceBinRef.current.contains(e.target)) setSourceBinOpen(false);
      if (destBinRef.current && !destBinRef.current.contains(e.target)) setDestBinOpen(false);
      if (itemRef.current && !itemRef.current.contains(e.target)) setItemOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  useEffect(() => {
    loadWarehouses();
    loadTransfers();
  }, []);

  // Reset source bin selection when source warehouse changes so a
  // stale (warehouse A, bin from B) state cannot reach the submit.
  useEffect(() => {
    setForm((f) => ({ ...f, source_bin_id: '' }));
    setSourceBinSearch('');
  }, [form.source_warehouse_id]);

  useEffect(() => {
    setForm((f) => ({ ...f, destination_bin_id: '' }));
    setDestBinSearch('');
  }, [form.destination_warehouse_id]);

  async function loadWarehouses() {
    const res = await api.get('/admin/warehouses', { silentPermissionDenied: true });
    if (res?.ok) {
      const data = await res.json();
      setWarehouses(data.warehouses || []);
    }
  }

  async function loadTransfers() {
    const res = await api.get('/admin/inter-warehouse-transfers?limit=50');
    if (res?.ok) {
      const data = await res.json();
      setTransfers(data.transfers || []);
    }
  }

  function updateField(key, value) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  function selectSourceBin(b) {
    setForm((f) => ({ ...f, source_bin_id: b.bin_id }));
    setSourceBinSearch(b.bin_code);
    setSourceBinOpen(false);
  }

  function selectDestBin(b) {
    setForm((f) => ({ ...f, destination_bin_id: b.bin_id }));
    setDestBinSearch(b.bin_code);
    setDestBinOpen(false);
  }

  function selectItem(i) {
    setForm((f) => ({ ...f, item_id: i.item_id }));
    setItemSearch(`${i.sku}  -  ${i.item_name}`);
    setItemOpen(false);
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    setSuccess('');

    if (!form.source_warehouse_id || !form.source_bin_id || !form.destination_warehouse_id || !form.destination_bin_id || !form.item_id || !form.quantity) {
      setError('All fields are required.');
      return;
    }

    if (Number(form.quantity) < 1) {
      setError('Quantity must be at least 1.');
      return;
    }

    setSubmitting(true);
    try {
      const body = {
        from_warehouse_id: Number(form.source_warehouse_id),
        from_bin_id: Number(form.source_bin_id),
        to_warehouse_id: Number(form.destination_warehouse_id),
        to_bin_id: Number(form.destination_bin_id),
        item_id: Number(form.item_id),
        quantity: Number(form.quantity),
      };
      const res = await api.post('/admin/inter-warehouse-transfer', body);
      if (res?.ok) {
        const data = await res.json();
        setSuccess(data.message || 'Transfer created successfully.');
        setForm({ source_warehouse_id: '', source_bin_id: '', destination_warehouse_id: '', destination_bin_id: '', item_id: '', quantity: '' });
        setSourceBinSearch('');
        setDestBinSearch('');
        setItemSearch('');
        loadTransfers();
      } else {
        const data = await res.json().catch(() => null);
        setError(data?.error || `Transfer failed (${res.status}).`);
      }
    } catch (err) {
      setError('Network error. Please try again.');
    } finally {
      setSubmitting(false);
    }
  }

  function formatDate(dateStr) {
    if (!dateStr) return '-';
    return new Date(dateStr).toLocaleString();
  }

  function statusTag(status) {
    const cls = status === 'completed' ? 'tag tag-success' : 'tag tag-info';
    return <span className={cls}>{status}</span>;
  }

  return (
    <div>
      <PageHeader title="Inter-Warehouse Transfers" />

      <div className="settings-section">
        <h3>Create Transfer</h3>
        {error && <div className="form-error" style={{ color: '#c0392b', marginBottom: 12 }}>{error}</div>}
        {success && <div className="form-success" style={{ color: '#27ae60', marginBottom: 12 }}>{success}</div>}

        <form onSubmit={handleSubmit}>
          <div className="form-row">
            <div className="form-group">
              <label>Source Warehouse</label>
              <select className="form-select" value={form.source_warehouse_id} onChange={(e) => updateField('source_warehouse_id', e.target.value)}>
                <option value="">Select warehouse...</option>
                {warehouses.map((w) => (
                  <option key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_name} ({w.warehouse_code})</option>
                ))}
              </select>
            </div>
            <div className="form-group" ref={sourceBinRef} style={{ position: 'relative' }}>
              <label>Source Bin {sourceBinQuery.searching && <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>(searching...)</span>}</label>
              <input
                className="form-input mono"
                placeholder={form.source_warehouse_id ? 'Type bin code to search' : 'Pick warehouse first'}
                value={sourceBinSearch}
                onChange={(e) => { setSourceBinSearch(e.target.value); updateField('source_bin_id', ''); setSourceBinOpen(true); }}
                onFocus={() => setSourceBinOpen(true)}
                disabled={!form.source_warehouse_id}
                autoComplete="off"
              />
              {sourceBinOpen && sourceBinQuery.results.length > 0 && (
                <div style={dropdownStyle}>
                  {sourceBinQuery.results.map((b) => (
                    <div key={b.bin_id} style={dropdownItemStyle} onMouseDown={() => selectSourceBin(b)}>
                      <span className="mono">{b.bin_code}</span> {b.bin_type ? `(${b.bin_type})` : ''}
                    </div>
                  ))}
                </div>
              )}
              {sourceBinOpen && !sourceBinQuery.searching && sourceBinSearch.trim().length >= 1 && !form.source_bin_id && sourceBinQuery.results.length === 0 && (
                <div style={{ ...dropdownStyle, padding: 12, fontSize: 12, color: 'var(--text-secondary)' }}>
                  No bins match "{sourceBinSearch.trim()}" in this warehouse.
                </div>
              )}
            </div>
          </div>

          <div className="form-row">
            <div className="form-group">
              <label>Destination Warehouse</label>
              <select className="form-select" value={form.destination_warehouse_id} onChange={(e) => updateField('destination_warehouse_id', e.target.value)}>
                <option value="">Select warehouse...</option>
                {warehouses.map((w) => (
                  <option key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_name} ({w.warehouse_code})</option>
                ))}
              </select>
            </div>
            <div className="form-group" ref={destBinRef} style={{ position: 'relative' }}>
              <label>Destination Bin {destBinQuery.searching && <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>(searching...)</span>}</label>
              <input
                className="form-input mono"
                placeholder={form.destination_warehouse_id ? 'Type bin code to search' : 'Pick warehouse first'}
                value={destBinSearch}
                onChange={(e) => { setDestBinSearch(e.target.value); updateField('destination_bin_id', ''); setDestBinOpen(true); }}
                onFocus={() => setDestBinOpen(true)}
                disabled={!form.destination_warehouse_id}
                autoComplete="off"
              />
              {destBinOpen && destBinQuery.results.length > 0 && (
                <div style={dropdownStyle}>
                  {destBinQuery.results.map((b) => (
                    <div key={b.bin_id} style={dropdownItemStyle} onMouseDown={() => selectDestBin(b)}>
                      <span className="mono">{b.bin_code}</span> {b.bin_type ? `(${b.bin_type})` : ''}
                    </div>
                  ))}
                </div>
              )}
              {destBinOpen && !destBinQuery.searching && destBinSearch.trim().length >= 1 && !form.destination_bin_id && destBinQuery.results.length === 0 && (
                <div style={{ ...dropdownStyle, padding: 12, fontSize: 12, color: 'var(--text-secondary)' }}>
                  No bins match "{destBinSearch.trim()}" in this warehouse.
                </div>
              )}
            </div>
          </div>

          <div className="form-row">
            <div className="form-group" ref={itemRef} style={{ position: 'relative' }}>
              <label>Item {itemSearching && <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>(searching...)</span>}</label>
              <input
                className="form-input"
                placeholder="Type SKU or item name (min 2 chars)"
                value={itemSearch}
                onChange={(e) => { setItemSearch(e.target.value); updateField('item_id', ''); setItemOpen(true); }}
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
            <div className="form-group">
              <label>Quantity</label>
              <input className="form-input" type="number" min="1" value={form.quantity} onChange={(e) => updateField('quantity', e.target.value)} placeholder="Qty" />
            </div>
          </div>

          <button className="btn btn-primary" type="submit" disabled={submitting}>
            {submitting ? 'Submitting...' : 'Create Transfer'}
          </button>
        </form>
      </div>

      <div className="settings-section" style={{ marginTop: 24 }}>
        <h3>Recent Transfers</h3>
        <div className="data-table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Item</th>
                <th>Qty</th>
                <th>From</th>
                <th>To</th>
                <th>Status</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {transfers.length === 0 && (
                <tr><td colSpan={7} style={{ textAlign: 'center', padding: 24, color: '#999' }}>No transfers found</td></tr>
              )}
              {transfers.map((t) => (
                <tr key={t.transfer_id || t.id}>
                  <td>{t.transfer_id || t.id}</td>
                  <td>{t.sku || t.item_name || t.item_id}</td>
                  <td>{t.quantity}</td>
                  <td>{t.from_warehouse_name || t.from_warehouse_code || t.from_warehouse_id} / {t.from_bin_code || t.from_bin_id}</td>
                  <td>{t.to_warehouse_name || t.to_warehouse_code || t.to_warehouse_id} / {t.to_bin_code || t.to_bin_id}</td>
                  <td>{statusTag(t.status || 'completed')}</td>
                  <td style={{ fontFamily: 'monospace' }}>{formatDate(t.transferred_at || t.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
