import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

export default function PreferredBins() {
  const [rows, setRows] = useState([]);
  const [search, setSearch] = useState('');
  const [showAdd, setShowAdd] = useState(false);
  const [addForm, setAddForm] = useState({ item_id: '', bin_id: '', priority: '1' });
  const [items, setItems] = useState([]);
  const [bins, setBins] = useState([]);
  const [editingId, setEditingId] = useState(null);
  const [editPriority, setEditPriority] = useState('');
  const [message, setMessage] = useState('');

  useEffect(() => { load(); }, [search]);

  async function load() {
    const params = new URLSearchParams();
    if (search) params.set('q', search);
    const res = await api.get(`/admin/preferred-bins?${params}`);
    if (res?.ok) {
      const data = await res.json();
      setRows(data.preferred_bins || []);
    }
  }

  async function openAdd() {
    const [itemRes, binRes] = await Promise.all([
      api.get('/admin/items?per_page=200'),
      api.get('/admin/bins?warehouse_id=1'),
    ]);
    if (itemRes?.ok) setItems((await itemRes.json()).items || []);
    if (binRes?.ok) setBins((await binRes.json()).bins || []);
    setAddForm({ item_id: '', bin_id: '', priority: '1' });
    setShowAdd(true);
  }

  async function saveAdd() {
    if (!addForm.item_id || !addForm.bin_id) return;
    const res = await api.post('/admin/preferred-bins', {
      item_id: Number(addForm.item_id),
      bin_id: Number(addForm.bin_id),
      priority: Number(addForm.priority) || 1,
    });
    if (res?.ok) {
      setShowAdd(false);
      setMessage('Preferred bin added');
      load();
    } else {
      const data = await res?.json();
      setMessage(data?.error || 'Failed to add');
    }
  }

  async function savePriority(pbId) {
    const val = Number(editPriority);
    if (!val || val < 1) return;
    const res = await api.put(`/admin/preferred-bins/${pbId}`, { priority: val });
    if (res?.ok) {
      setEditingId(null);
      load();
    }
  }

  async function deletePB(pbId) {
    if (!confirm('Delete this preferred bin?')) return;
    const res = await api.delete(`/admin/preferred-bins/${pbId}`);
    if (res?.ok) load();
  }

  function exportCSV() {
    const header = 'SKU,Item Name,Bin Code,Zone,Priority,Last Updated';
    const csvRows = rows.map((r) =>
      `"${r.sku}","${r.item_name}","${r.bin_code}","${r.zone_name || ''}",${r.priority},"${r.updated_at || ''}"`
    );
    const blob = new Blob([header + '\n' + csvRows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'preferred_bins.csv';
    a.click();
    URL.revokeObjectURL(url);
  }

  const columns = [
    { key: 'sku', label: 'SKU', mono: true },
    { key: 'item_name', label: 'Item Name' },
    { key: 'bin_code', label: 'Preferred Bin', mono: true },
    { key: 'zone_name', label: 'Zone', render: (r) => r.zone_name || '-' },
    { key: 'priority', label: 'Priority', render: (r) => {
      if (editingId === r.preferred_bin_id) {
        return (
          <span style={{ display: 'flex', gap: 4 }}>
            <input
              className="form-input"
              type="number"
              min="1"
              value={editPriority}
              onChange={(e) => setEditPriority(e.target.value)}
              style={{ width: 50, padding: '2px 6px', fontSize: 13 }}
              onKeyDown={(e) => e.key === 'Enter' && savePriority(r.preferred_bin_id)}
            />
            <button className="btn btn-sm" onClick={() => savePriority(r.preferred_bin_id)}>OK</button>
          </span>
        );
      }
      return (
        <span
          style={{ cursor: 'pointer', textDecoration: 'underline dotted' }}
          onClick={() => { setEditingId(r.preferred_bin_id); setEditPriority(String(r.priority)); }}
        >
          {r.priority}
        </span>
      );
    }},
    { key: 'updated_at', label: 'Updated', mono: true, render: (r) => r.updated_at ? new Date(r.updated_at).toLocaleDateString() : '-' },
    { key: 'actions', label: '', render: (r) => (
      <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); deletePB(r.preferred_bin_id); }}>Delete</button>
    )},
  ];

  return (
    <div>
      <PageHeader title="Preferred Bins">
        <button className="btn" onClick={exportCSV}>Export CSV</button>
        <button className="btn btn-primary" onClick={openAdd}>Add Preferred Bin</button>
      </PageHeader>

      {message && (
        <div style={{ marginBottom: 12, fontSize: 13, color: 'var(--success)' }}>{message}</div>
      )}

      <div className="filter-bar">
        <input
          className="form-input"
          placeholder="Search by SKU or item name..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      <DataTable columns={columns} data={rows} emptyMessage="No preferred bins configured" />

      {showAdd && (
        <Modal
          title="Add Preferred Bin"
          onClose={() => setShowAdd(false)}
          footer={
            <>
              <button className="btn" onClick={() => setShowAdd(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={saveAdd} disabled={!addForm.item_id || !addForm.bin_id}>Save</button>
            </>
          }
        >
          <div className="form-group">
            <label>Item</label>
            <select className="form-select" value={addForm.item_id} onChange={(e) => setAddForm({ ...addForm, item_id: e.target.value })}>
              <option value="">Select item...</option>
              {items.map((it) => (
                <option key={it.item_id} value={it.item_id}>{it.sku} - {it.item_name}</option>
              ))}
            </select>
          </div>
          <div className="form-group">
            <label>Bin</label>
            <select className="form-select" value={addForm.bin_id} onChange={(e) => setAddForm({ ...addForm, bin_id: e.target.value })}>
              <option value="">Select bin...</option>
              {bins.map((b) => (
                <option key={b.id} value={b.id}>{b.bin_code} - {b.zone_name || ''}</option>
              ))}
            </select>
          </div>
          <div className="form-group">
            <label>Priority</label>
            <input className="form-input" type="number" min="1" value={addForm.priority} onChange={(e) => setAddForm({ ...addForm, priority: e.target.value })} style={{ width: 80 }} />
          </div>
        </Modal>
      )}
    </div>
  );
}
