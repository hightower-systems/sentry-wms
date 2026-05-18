import { useEffect, useState } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

const FILTER_OPTIONS = [
  { label: 'Active', value: 'active' },
  { label: 'Archived', value: 'archived' },
  { label: 'All', value: 'all' },
];

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

export default function Vendors() {
  const [vendors, setVendors] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('active');
  const [showModal, setShowModal] = useState(false);
  const [editId, setEditId] = useState(null);
  const [form, setForm] = useState({});
  const [error, setError] = useState('');
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deleteError, setDeleteError] = useState('');

  useEffect(() => { loadVendors(); }, [page, search, filter]); // eslint-disable-line react-hooks/exhaustive-deps

  async function loadVendors() {
    const params = new URLSearchParams({ page: String(page), per_page: '50' });
    if (search) params.set('q', search);
    if (filter === 'active') params.set('active', 'true');
    else if (filter === 'archived') params.set('active', 'false');
    const res = await api.get(`/admin/vendors?${params}`);
    if (!res?.ok) return;
    const data = await res.json();
    setVendors(data.vendors || []);
    setPagination({ page: data.page, pages: data.pages, total: data.total, per_page: data.per_page });
  }

  function openCreate() {
    setEditId(null);
    setForm({ vendor_name: '', contact_name: '', email: '', phone: '', tax_id: '', payment_terms: '', is_active: true });
    setError('');
    setShowModal(true);
  }

  function openEdit(vendor) {
    setEditId(vendor.canonical_id);
    setForm({
      vendor_name: vendor.vendor_name || '',
      contact_name: vendor.contact_name || '',
      email: vendor.email || '',
      phone: vendor.phone || '',
      billing_address: vendor.billing_address || '',
      remit_to_address: vendor.remit_to_address || '',
      tax_id: vendor.tax_id || '',
      payment_terms: vendor.payment_terms || '',
      is_active: vendor.is_active !== false,
    });
    setError('');
    setShowModal(true);
  }

  async function save() {
    setError('');
    const body = {
      vendor_name: form.vendor_name,
      contact_name: form.contact_name || null,
      email: form.email || null,
      phone: form.phone || null,
      billing_address: form.billing_address || null,
      remit_to_address: form.remit_to_address || null,
      tax_id: form.tax_id || null,
      payment_terms: form.payment_terms || null,
      is_active: !!form.is_active,
    };
    const res = editId
      ? await api.put(`/admin/vendors/${editId}`, body)
      : await api.post('/admin/vendors', body);
    if (res?.ok) {
      setShowModal(false);
      loadVendors();
    } else {
      let data = null;
      try { data = await res?.json(); } catch (_) { /* non-JSON body */ }
      setError(formatApiError(data, 'Failed to save'));
    }
  }

  async function confirmDelete() {
    setDeleteError('');
    if (!deleteTarget) return;
    const res = await api.delete(`/admin/vendors/${deleteTarget.canonical_id}`);
    if (res?.ok) {
      setDeleteTarget(null);
      loadVendors();
    } else {
      let data = null;
      try { data = await res?.json(); } catch (_) { /* non-JSON body */ }
      setDeleteError(formatApiError(data, 'Failed to delete'));
    }
  }

  const columns = [
    { key: 'vendor_name', label: 'Vendor', sortable: true },
    { key: 'contact_name', label: 'Contact', render: (r) => r.contact_name || '-' },
    { key: 'email', label: 'Email', render: (r) => r.email || '-' },
    { key: 'phone', label: 'Phone', mono: true, render: (r) => r.phone || '-' },
    { key: 'is_active', label: 'Active', render: (r) => (r.is_active === false ? 'No' : 'Yes') },
    {
      key: 'actions', label: '', render: (r) => (
        <div style={{ display: 'flex', gap: 4 }}>
          <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); openEdit(r); }} aria-label="Edit" title="Edit">&#9998;</button>
          <button
            className="btn btn-sm btn-danger"
            onClick={(e) => { e.stopPropagation(); setDeleteTarget(r); setDeleteError(''); }}
            aria-label="Delete" title="Delete (only when no items reference this vendor)"
          >&#128465;</button>
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title="Vendors">
        <button className="btn btn-primary" onClick={openCreate}>New Vendor</button>
      </PageHeader>

      <div className="filter-bar">
        <input
          className="form-input"
          placeholder="Search by name, contact, email"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
          style={{ maxWidth: 320 }}
        />
        <select
          className="form-select"
          value={filter}
          onChange={(e) => { setFilter(e.target.value); setPage(1); }}
        >
          {FILTER_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </div>

      <DataTable
        columns={columns}
        data={vendors}
        pagination={pagination}
        onPageChange={setPage}
        onRowClick={openEdit}
        emptyMessage="No vendors found"
      />

      {showModal && (
        <Modal
          title={editId ? 'Edit Vendor' : 'New Vendor'}
          onClose={() => setShowModal(false)}
          footer={
            <>
              <button className="btn" onClick={() => setShowModal(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={save}>Save</button>
            </>
          }
          size="wide"
        >
          {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
          <div className="form-row">
            <div className="form-group">
              <label>Vendor Name</label>
              <input
                className="form-input" autoFocus
                value={form.vendor_name || ''}
                onChange={(e) => setForm({ ...form, vendor_name: e.target.value })}
              />
            </div>
            <div className="form-group">
              <label>Contact Name</label>
              <input
                className="form-input"
                value={form.contact_name || ''}
                onChange={(e) => setForm({ ...form, contact_name: e.target.value })}
              />
            </div>
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>Email</label>
              <input
                className="form-input" type="email"
                value={form.email || ''}
                onChange={(e) => setForm({ ...form, email: e.target.value })}
              />
            </div>
            <div className="form-group">
              <label>Phone</label>
              <input
                className="form-input"
                value={form.phone || ''}
                onChange={(e) => setForm({ ...form, phone: e.target.value })}
              />
            </div>
          </div>
          <div className="form-group">
            <label>Billing Address</label>
            <textarea
              className="form-input" rows={2}
              value={form.billing_address || ''}
              onChange={(e) => setForm({ ...form, billing_address: e.target.value })}
            />
          </div>
          <div className="form-group">
            <label>Remit-To Address</label>
            <textarea
              className="form-input" rows={2}
              value={form.remit_to_address || ''}
              onChange={(e) => setForm({ ...form, remit_to_address: e.target.value })}
            />
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>Tax ID</label>
              <input
                className="form-input"
                value={form.tax_id || ''}
                onChange={(e) => setForm({ ...form, tax_id: e.target.value })}
              />
            </div>
            <div className="form-group">
              <label>Payment Terms</label>
              <input
                className="form-input"
                placeholder="e.g. Net 30"
                value={form.payment_terms || ''}
                onChange={(e) => setForm({ ...form, payment_terms: e.target.value })}
              />
            </div>
          </div>
          <div className="form-group">
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={form.is_active !== false}
                onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
              />
              <span>Active (uncheck to hide from item / PO vendor pickers)</span>
            </label>
          </div>
        </Modal>
      )}

      {deleteTarget && (
        <Modal
          title={`Delete vendor ${deleteTarget.vendor_name}?`}
          onClose={() => { setDeleteTarget(null); setDeleteError(''); }}
          footer={
            <>
              <button className="btn" onClick={() => { setDeleteTarget(null); setDeleteError(''); }}>Cancel</button>
              <button className="btn btn-danger" onClick={confirmDelete}>Delete</button>
            </>
          }
        >
          {deleteError && <div className="form-error" style={{ marginBottom: 12 }}>{deleteError}</div>}
          <p style={{ fontSize: 13 }}>
            Permanently delete this vendor. The action is blocked when any item still
            references this vendor; soft-archive via the Active checkbox in the edit form
            for that case.
          </p>
        </Modal>
      )}
    </div>
  );
}
