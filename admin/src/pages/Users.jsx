import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useAuth } from '../auth.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

const ROLES = ['ADMIN', 'MANAGER', 'PICKER', 'PACKER', 'RECEIVER'];

export default function Users() {
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState([]);
  const [showModal, setShowModal] = useState(false);
  const [editId, setEditId] = useState(null);
  const [form, setForm] = useState({});
  const [error, setError] = useState('');

  useEffect(() => { loadUsers(); }, []);

  async function loadUsers() {
    const res = await api.get('/admin/users');
    if (res?.ok) {
      const data = await res.json();
      setUsers(data.users || []);
    }
  }

  function openCreate() {
    setEditId(null);
    setForm({ role: 'PICKER', warehouse_id: 1, is_active: true });
    setError('');
    setShowModal(true);
  }

  function openEdit(user) {
    setEditId(user.id);
    setForm({ ...user, password: '' });
    setError('');
    setShowModal(true);
  }

  async function save() {
    setError('');
    const body = { username: form.username, full_name: form.full_name, role: form.role, warehouse_id: form.warehouse_id ? Number(form.warehouse_id) : null };
    if (form.password) body.password = form.password;
    if (!editId) body.password = form.password;
    const res = editId
      ? await api.put(`/admin/users/${editId}`, body)
      : await api.post('/admin/users', body);
    if (res?.ok) {
      setShowModal(false);
      loadUsers();
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to save');
    }
  }

  async function deactivate(id) {
    if (id === currentUser?.id) { alert('Cannot deactivate yourself'); return; }
    if (!confirm('Deactivate this user?')) return;
    const res = await api.delete(`/admin/users/${id}`);
    if (res?.ok) {
      loadUsers();
    } else {
      const data = await res?.json();
      alert(data?.error || 'Failed to deactivate');
    }
  }

  const columns = [
    { key: 'username', label: 'Username', mono: true },
    { key: 'full_name', label: 'Full Name' },
    { key: 'role', label: 'Role' },
    { key: 'warehouse_id', label: 'Warehouse', render: (r) => r.warehouse_id || '-' },
    { key: 'is_active', label: 'Active', render: (r) => r.is_active ? 'Yes' : 'No' },
    { key: 'actions', label: '', render: (r) => (
      <div style={{ display: 'flex', gap: 4 }}>
        <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); openEdit(r); }}>Edit</button>
        {r.id !== currentUser?.id && r.is_active && (
          <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); deactivate(r.id); }}>Deactivate</button>
        )}
      </div>
    )},
  ];

  return (
    <div>
      <PageHeader title="Users">
        <button className="btn btn-primary" onClick={openCreate}>New User</button>
      </PageHeader>
      <DataTable columns={columns} data={users} emptyMessage="No users found" />

      {showModal && (
        <Modal title={editId ? 'Edit User' : 'New User'} onClose={() => setShowModal(false)}
          footer={
            <>
              <button className="btn" onClick={() => setShowModal(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={save}>Save</button>
            </>
          }
        >
          {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
          <div className="form-row">
            <div className="form-group">
              <label>Username</label>
              <input className="form-input" value={form.username || ''} onChange={(e) => setForm({ ...form, username: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Full Name</label>
              <input className="form-input" value={form.full_name || ''} onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
            </div>
          </div>
          <div className="form-group">
            <label>{editId ? 'New Password (leave blank to keep current)' : 'Password'}</label>
            <input className="form-input" type="password" value={form.password || ''} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>Role</label>
              <select className="form-select" value={form.role || ''} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
            <div className="form-group">
              <label>Warehouse ID</label>
              <input className="form-input" type="number" value={form.warehouse_id ?? ''} onChange={(e) => setForm({ ...form, warehouse_id: e.target.value })} />
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
