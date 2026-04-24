import { useState, useEffect } from 'react';
import { api } from '../api.js';
import PageHeader from '../components/PageHeader.jsx';

export default function InterWarehouseTransfers() {
  const [warehouses, setWarehouses] = useState([]);
  const [sourceBins, setSourceBins] = useState([]);
  const [destBins, setDestBins] = useState([]);
  const [items, setItems] = useState([]);
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

  useEffect(() => {
    loadWarehouses();
    loadTransfers();
  }, []);

  // Load source bins and items when source warehouse changes
  useEffect(() => {
    if (form.source_warehouse_id) {
      loadBins(form.source_warehouse_id, 'source');
      loadItems(form.source_warehouse_id);
    } else {
      setSourceBins([]);
      setItems([]);
    }
    setForm((f) => ({ ...f, source_bin_id: '', item_id: '' }));
  }, [form.source_warehouse_id]);

  // Load dest bins when destination warehouse changes
  useEffect(() => {
    if (form.destination_warehouse_id) {
      loadBins(form.destination_warehouse_id, 'dest');
    } else {
      setDestBins([]);
    }
    setForm((f) => ({ ...f, destination_bin_id: '' }));
  }, [form.destination_warehouse_id]);

  async function loadWarehouses() {
    const res = await api.get('/admin/warehouses');
    if (res?.ok) {
      const data = await res.json();
      setWarehouses(data.warehouses || []);
    }
  }

  async function loadBins(warehouseId, target) {
    const res = await api.get(`/admin/bins?warehouse_id=${warehouseId}`);
    if (res?.ok) {
      const data = await res.json();
      if (target === 'source') setSourceBins(data.bins || []);
      else setDestBins(data.bins || []);
    }
  }

  async function loadItems(warehouseId) {
    const res = await api.get(`/admin/items?warehouse_id=${warehouseId}&per_page=1000`);
    if (res?.ok) {
      const data = await res.json();
      setItems(data.items || []);
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
            <div className="form-group">
              <label>Source Bin</label>
              <select className="form-select" value={form.source_bin_id} onChange={(e) => updateField('source_bin_id', e.target.value)} disabled={!form.source_warehouse_id}>
                <option value="">Select bin...</option>
                {sourceBins.map((b) => (
                  <option key={b.bin_id} value={b.bin_id}>{b.bin_code} ({b.bin_type})</option>
                ))}
              </select>
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
            <div className="form-group">
              <label>Destination Bin</label>
              <select className="form-select" value={form.destination_bin_id} onChange={(e) => updateField('destination_bin_id', e.target.value)} disabled={!form.destination_warehouse_id}>
                <option value="">Select bin...</option>
                {destBins.map((b) => (
                  <option key={b.bin_id} value={b.bin_id}>{b.bin_code} ({b.bin_type})</option>
                ))}
              </select>
            </div>
          </div>

          <div className="form-row">
            <div className="form-group">
              <label>Item</label>
              <select className="form-select" value={form.item_id} onChange={(e) => updateField('item_id', e.target.value)} disabled={!form.source_warehouse_id}>
                <option value="">Select item...</option>
                {items.map((i) => (
                  <option key={i.item_id} value={i.item_id}>{i.sku}  -  {i.item_name}</option>
                ))}
              </select>
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
