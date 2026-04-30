import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

const FILTER_OPTIONS = [
  { label: 'Active', value: 'active' },
  { label: 'Archived', value: 'archived' },
  { label: 'All', value: 'all' },
];

export default function Items() {
  const [searchParams] = useSearchParams();
  const [items, setItems] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState(searchParams.get('q') || '');
  const [filter, setFilter] = useState('active');
  const [showModal, setShowModal] = useState(false);
  const [editId, setEditId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [form, setForm] = useState({});
  const [error, setError] = useState('');

  useEffect(() => { loadItems(); }, [page, search, filter]);

  async function loadItems() {
    const params = new URLSearchParams({ page, per_page: 50 });
    if (search) params.set('q', search);
    if (filter === 'active') params.set('active', 'true');
    else if (filter === 'archived') params.set('active', 'false');
    const res = await api.get(`/admin/items?${params}`);
    if (res?.ok) {
      const data = await res.json();
      const mapped = (data.items || []).map((item) => ({
        ...item,
        id: item.id || item.item_id,
      }));
      setItems(mapped);
      setPagination({ page: data.page, pages: data.pages, total: data.total, per_page: data.per_page });
    }
  }

  async function viewItem(item) {
    const res = await api.get(`/admin/items/${item.id}`);
    if (res?.ok) {
      const data = await res.json();
      const itemData = data.item || data;
      setDetail({
        ...itemData,
        id: itemData.id || itemData.item_id,
        inventory: data.inventory || itemData.inventory || [],
        preferred_bins: data.preferred_bins || itemData.preferred_bins || [],
      });
    }
  }

  function openCreate() {
    setEditId(null);
    setForm({ is_active: true });
    setError('');
    setShowModal(true);
  }

  function openEdit(item) {
    setEditId(item.id || item.item_id);
    setForm({ ...item, id: item.id || item.item_id });
    setError('');
    setShowModal(true);
  }

  async function save() {
    setError('');
    const body = {
      sku: form.sku,
      item_name: form.item_name,
      upc: form.upc || null,
      category: form.category || null,
      weight_lbs: (form.weight_lbs || form.weight) ? Number(form.weight_lbs || form.weight) : null,
      default_bin_id: form.default_bin_id ? Number(form.default_bin_id) : null,
    };
    const res = editId
      ? await api.put(`/admin/items/${editId}`, body)
      : await api.post('/admin/items', body);
    if (res?.ok) {
      setShowModal(false);
      setDetail(null);
      loadItems();
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to save');
    }
  }

  const [showDeleteConfirm, setShowDeleteConfirm] = useState(null);

  async function deleteItem(id) {
    setShowDeleteConfirm(id);
  }

  async function confirmDeleteItem() {
    const id = showDeleteConfirm;
    setShowDeleteConfirm(null);
    const res = await api.delete(`/admin/items/${id}`);
    if (res?.ok) {
      setDetail(null);
      setShowModal(false);
      loadItems();
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to delete item');
    }
  }

  async function toggleArchive(item) {
    const res = await api.post(`/admin/items/${item.id}/archive`);
    if (res?.ok) {
      setDetail(null);
      loadItems();
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to update item');
    }
  }

  const columns = [
    { key: 'sku', label: 'SKU', mono: true },
    { key: 'item_name', label: 'Item Name' },
    { key: 'upc', label: 'UPC', mono: true, render: (r) => r.upc || '-' },
    { key: 'default_bin_code', label: 'Default Bin', mono: true, render: (r) => r.default_bin_code || '\u2013' },
    { key: 'category', label: 'Category', render: (r) => r.category || '-' },
    { key: 'weight_lbs', label: 'Weight', render: (r) => r.weight_lbs ? `${r.weight_lbs} lb` : '-' },
    { key: 'is_active', label: 'Active', render: (r) => r.is_active ? 'Yes' : 'No' },
    { key: 'actions', label: '', render: (r) => (
      <div style={{ display: 'flex', gap: 4 }}>
        <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); openEdit(r); }} aria-label="Edit" title="Edit">&#9998;</button>
        <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); deleteItem(r.id || r.item_id); }} aria-label="Delete" title="Delete">&#128465;</button>
      </div>
    )},
  ];

  const invCols = [
    { key: 'bin_code', label: 'Bin', mono: true },
    { key: 'quantity_on_hand', label: 'On Hand' },
    { key: 'quantity_allocated', label: 'Allocated' },
  ];

  return (
    <div>
      <PageHeader title="Items">
        <button className="btn btn-primary" onClick={openCreate}>New Item</button>
      </PageHeader>
      <div className="filter-bar">
        <input className="form-input" placeholder="Search by SKU, name, or UPC..." value={search} onChange={(e) => { setSearch(e.target.value); setPage(1); }} />
        <select
          className="form-select"
          value={filter}
          onChange={(e) => { setFilter(e.target.value); setPage(1); }}
          style={{ width: 'auto', minWidth: 120 }}
        >
          {FILTER_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </div>
      <DataTable columns={columns} data={items} pagination={pagination} onPageChange={setPage} onRowClick={viewItem} />

      {detail && !showModal && (
        <Modal title={detail.item_name || detail.sku} onClose={() => setDetail(null)}
          footer={<button className="btn" onClick={() => setDetail(null)}>Close</button>}
        >
          <div className="detail-grid">
            <span className="detail-label">SKU</span><span className="mono">{detail.sku}</span>
            <span className="detail-label">UPC</span><span className="mono">{detail.upc || '-'}</span>
            <span className="detail-label">Category</span><span>{detail.category || '-'}</span>
            <span className="detail-label">Weight</span><span>{(detail.weight_lbs || detail.weight) ? `${detail.weight_lbs || detail.weight} lb` : '-'}</span>
            <span className="detail-label">Active</span><span>{detail.is_active ? 'Yes' : 'No'}</span>
          </div>
          {detail.preferred_bins && detail.preferred_bins.length > 0 && (
            <>
              <div className="section-title">Preferred Bins</div>
              <DataTable columns={[
                { key: 'bin_code', label: 'Bin', mono: true },
                { key: 'zone_name', label: 'Zone' },
                { key: 'priority', label: 'Priority' },
              ]} data={detail.preferred_bins} />
            </>
          )}
          {detail.inventory && detail.inventory.length > 0 && (
            <>
              <div className="section-title">Inventory locations</div>
              <DataTable columns={invCols} data={detail.inventory} />
            </>
          )}
        </Modal>
      )}

      {showModal && (
        <Modal title={editId ? 'Edit Item' : 'New Item'} onClose={() => setShowModal(false)}
          footer={
            <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%' }}>
              <div style={{ display: 'flex', gap: 4 }}>
                {editId && (
                  <button className="btn btn-sm" onClick={() => toggleArchive(form)}>
                    {form.is_active ? 'Archive' : 'Restore'}
                  </button>
                )}
              </div>
              <div style={{ display: 'flex', gap: 4 }}>
                <button className="btn" onClick={() => setShowModal(false)}>Cancel</button>
                <button className="btn btn-primary" onClick={save}>Save</button>
              </div>
            </div>
          }
        >
          {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
          <div className="form-row">
            <div className="form-group">
              <label>SKU</label>
              <input className="form-input" value={form.sku || ''} onChange={(e) => setForm({ ...form, sku: e.target.value })} />
            </div>
            <div className="form-group">
              <label>UPC</label>
              <input className="form-input" value={form.upc || ''} onChange={(e) => setForm({ ...form, upc: e.target.value })} />
            </div>
          </div>
          <div className="form-group">
            <label>Item Name</label>
            <input className="form-input" value={form.item_name || ''} onChange={(e) => setForm({ ...form, item_name: e.target.value })} />
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>Category</label>
              <input className="form-input" value={form.category || ''} onChange={(e) => setForm({ ...form, category: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Weight (lb)</label>
              <input className="form-input" type="number" step="0.01" value={form.weight_lbs ?? form.weight ?? ''} onChange={(e) => setForm({ ...form, weight_lbs: e.target.value, weight: e.target.value })} />
            </div>
          </div>
        </Modal>
      )}

      {showDeleteConfirm && (
        <Modal title="Delete Item" onClose={() => setShowDeleteConfirm(null)}
          footer={
            <>
              <button className="btn" onClick={() => setShowDeleteConfirm(null)}>Cancel</button>
              <button className="btn btn-danger" onClick={confirmDeleteItem}>Delete</button>
            </>
          }
        >
          <p style={{ fontSize: 14, marginBottom: 8 }}>Are you sure? This action cannot be undone.</p>
          <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>The item and all associated data will be permanently deleted.</p>
        </Modal>
      )}
    </div>
  );
}
