import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useAuth } from '../auth.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

const ROLES = ['ADMIN', 'USER'];

const ALL_FUNCTIONS = [
  { key: 'pick', label: 'Pick' },
  { key: 'pack', label: 'Pack' },
  { key: 'ship', label: 'Ship' },
  { key: 'receive', label: 'Receive' },
  { key: 'putaway', label: 'Put-Away' },
  { key: 'count', label: 'Count' },
  { key: 'transfer', label: 'Transfer' },
];

export default function Users() {
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState([]);
  const [warehouses, setWarehouses] = useState([]);
  const [showModal, setShowModal] = useState(false);
  const [editId, setEditId] = useState(null);
  const [form, setForm] = useState({});
  const [error, setError] = useState('');

  useEffect(() => {
    loadUsers();
    loadWarehouses();
  }, []);

  async function loadUsers() {
    const res = await api.get('/admin/users');
    if (res?.ok) {
      const data = await res.json();
      setUsers(data.users || []);
    }
  }

  async function loadWarehouses() {
    const res = await api.get('/admin/warehouses');
    if (res?.ok) {
      const data = await res.json();
      setWarehouses(data.warehouses || []);
    }
  }

  function openCreate() {
    setEditId(null);
    setForm({ role: 'USER', warehouse_ids: [], allowed_functions: [], is_active: true });
    setError('');
    setShowModal(true);
  }

  function openEdit(user) {
    setEditId(user.user_id);
    setForm({
      ...user,
      password: '',
      warehouse_ids: user.warehouse_ids || [],
      allowed_functions: user.allowed_functions || [],
    });
    setError('');
    setShowModal(true);
  }

  async function save() {
    setError('');
    const body = {
      username: form.username,
      full_name: form.full_name,
      role: form.role,
      warehouse_ids: form.warehouse_ids || [],
      allowed_functions: form.allowed_functions || [],
    };
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

  const [showDeleteConfirm, setShowDeleteConfirm] = useState(null);

  async function deleteUser(id) {
    if (id === currentUser?.user_id) { setError('Cannot delete yourself'); return; }
    setShowDeleteConfirm(id);
  }

  async function confirmDeleteUser() {
    const id = showDeleteConfirm;
    setShowDeleteConfirm(null);
    const res = await api.delete(`/admin/users/${id}`);
    if (res?.ok) {
      loadUsers();
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to delete user');
    }
  }

  function toggleWarehouse(whId) {
    const ids = form.warehouse_ids || [];
    if (ids.includes(whId)) {
      setForm({ ...form, warehouse_ids: ids.filter((id) => id !== whId) });
    } else {
      setForm({ ...form, warehouse_ids: [...ids, whId] });
    }
  }

  function toggleFunction(fn) {
    const fns = form.allowed_functions || [];
    if (fns.includes(fn)) {
      setForm({ ...form, allowed_functions: fns.filter((f) => f !== fn) });
    } else {
      setForm({ ...form, allowed_functions: [...fns, fn] });
    }
  }

  function warehouseCodes(warehouseIds) {
    if (!warehouseIds || warehouseIds.length === 0) return '-';
    return warehouseIds
      .map((id) => {
        const wh = warehouses.find((w) => w.warehouse_id === id);
        return wh ? wh.warehouse_code : id;
      })
      .join(', ');
  }

  const columns = [
    { key: 'username', label: 'Username', mono: true },
    { key: 'full_name', label: 'Full Name' },
    { key: 'role', label: 'Role' },
    { key: 'warehouse_ids', label: 'Warehouses', render: (r) => warehouseCodes(r.warehouse_ids) },
    { key: 'is_active', label: 'Active', render: (r) => r.is_active ? 'Yes' : 'No' },
    { key: 'actions', label: '', render: (r) => (
      <div style={{ display: 'flex', gap: 4 }}>
        <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); openEdit(r); }}>Edit</button>
        {r.user_id !== currentUser?.user_id && (
          <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); deleteUser(r.user_id); }}>Delete</button>
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
          <div className="form-group">
            <label>Role</label>
            <select className="form-select" value={form.role || ''} onChange={(e) => setForm({ ...form, role: e.target.value })}>
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </div>
          <div className="form-group">
            <label>Warehouses</label>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '8px 0' }}>
              {warehouses.map((wh) => (
                <label key={wh.warehouse_id} style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={(form.warehouse_ids || []).includes(wh.warehouse_id)}
                    onChange={() => toggleWarehouse(wh.warehouse_id)}
                  />
                  <span className="mono">{wh.warehouse_code}</span>
                  <span style={{ color: 'var(--text-secondary)' }}>{wh.warehouse_name}</span>
                </label>
              ))}
              {warehouses.length === 0 && <span style={{ color: 'var(--text-secondary)' }}>No warehouses found</span>}
            </div>
          </div>
          <div className="form-group">
            <label>Mobile Module Access</label>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, padding: '8px 0' }}>
              {ALL_FUNCTIONS.map((fn) => (
                <label key={fn.key} style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', minWidth: 100 }}>
                  <input
                    type="checkbox"
                    checked={(form.allowed_functions || []).includes(fn.key)}
                    onChange={() => toggleFunction(fn.key)}
                  />
                  {fn.label}
                </label>
              ))}
            </div>
          </div>
        </Modal>
      )}

      {showDeleteConfirm && (
        <Modal title="Delete User" onClose={() => setShowDeleteConfirm(null)}
          footer={
            <>
              <button className="btn" onClick={() => setShowDeleteConfirm(null)}>Cancel</button>
              <button className="btn btn-danger" onClick={confirmDeleteUser}>Delete</button>
            </>
          }
        >
          <p style={{ fontSize: 14, marginBottom: 8 }}>Are you sure? This action cannot be undone.</p>
          <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>The user and all associated data will be permanently deleted.</p>
        </Modal>
      )}
    </div>
  );
}
