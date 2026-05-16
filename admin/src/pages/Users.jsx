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

// Web-admin page grants (avid-overhaul-mk1 P6.1). Mirrors the sidebar
// groups so the create / edit modal feels familiar; keys match
// api/constants.py ALL_PAGE_KEYS exactly. ADMIN users bypass the
// permission table server-side, so checking these for an ADMIN is
// purely cosmetic - the PUT endpoint no-ops for ADMIN targets.
const PAGE_GROUPS = [
  {
    label: 'Floor',
    pages: [
      { key: 'inventory', label: 'Inventory' },
      { key: 'cycle-counts', label: 'Cycle Counts' },
      { key: 'count-approvals', label: 'Count Approvals' },
    ],
  },
  {
    label: 'Inbound',
    pages: [
      { key: 'purchase-orders', label: 'Purchase Orders' },
      { key: 'receiving', label: 'Receiving' },
      { key: 'putaway', label: 'Put-away' },
    ],
  },
  {
    label: 'Outbound',
    pages: [
      { key: 'sales-orders', label: 'Sales Orders' },
      { key: 'pos-activity', label: 'POS Activity' },
      { key: 'picking-tickets', label: 'Picking Tickets' },
      { key: 'fraud', label: 'Fraud' },
      { key: 'picking', label: 'Picking' },
      { key: 'packing', label: 'Packing' },
      { key: 'shipping', label: 'Shipping' },
    ],
  },
  {
    label: 'Warehouse',
    pages: [
      { key: 'items', label: 'Items' },
      { key: 'vendors', label: 'Vendors' },
      { key: 'adjustments', label: 'Inventory Adjustments' },
      { key: 'inter-warehouse-transfers', label: 'Inventory Transfers' },
      { key: 'transfer-orders', label: 'Transfer Orders' },
      { key: 'warehouses', label: 'Warehouses' },
      { key: 'bins', label: 'Bins' },
      { key: 'zones', label: 'Zones' },
      { key: 'preferred-bins', label: 'Preferred Bins' },
    ],
  },
  {
    label: 'System',
    pages: [
      { key: 'users', label: 'Users' },
      { key: 'api-tokens', label: 'API Tokens' },
      { key: 'inbound', label: 'Inbound Activity' },
      { key: 'consumer-groups', label: 'Consumer Groups' },
      { key: 'webhooks', label: 'Webhooks' },
      { key: 'audit-log', label: 'Audit Log' },
      { key: 'imports', label: 'Imports' },
      { key: 'integrations', label: 'Integrations' },
      { key: 'settings', label: 'Settings' },
    ],
  },
  // avid-overhaul-mk1 P7: feature-flag grants. Not pages in the
  // sidebar; granting one lifts the OPEN-only edit gate on the
  // matching admin surface (PO header/line edits past CLOSED+ARCHIVED;
  // SO header/line edits past OPEN). Same storage as page grants so
  // the multi-select reuses the existing pagePermissions array.
  {
    label: 'Overrides',
    isOverride: true,
    pages: [
      {
        key: 'po-full-edit',
        label: 'Full PO edit (past CLOSED/ARCHIVED)',
      },
      {
        key: 'so-full-edit',
        label: 'Full SO edit (past OPEN, incl. source_system + line CRUD)',
      },
    ],
  },
];

const ALL_PAGE_KEYS = PAGE_GROUPS.flatMap((g) => g.pages.map((p) => p.key));

export default function Users() {
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState([]);
  const [warehouses, setWarehouses] = useState([]);
  const [showModal, setShowModal] = useState(false);
  const [editId, setEditId] = useState(null);
  const [form, setForm] = useState({});
  const [error, setError] = useState('');
  // mig 059 P6.1: per-page web-admin grants. Edited inline alongside
  // the rest of the user form and saved via a follow-up PUT once the
  // user record itself lands.
  const [pagePermissions, setPagePermissions] = useState([]);

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
    const res = await api.get('/admin/warehouses', { silentPermissionDenied: true });
    if (res?.ok) {
      const data = await res.json();
      setWarehouses(data.warehouses || []);
    }
  }

  function openCreate() {
    setEditId(null);
    setForm({ role: 'USER', warehouse_ids: [], allowed_functions: [], is_active: true });
    setPagePermissions([]);
    setError('');
    setShowModal(true);
  }

  async function openEdit(user) {
    setEditId(user.user_id);
    setForm({
      ...user,
      password: '',
      warehouse_ids: user.warehouse_ids || [],
      allowed_functions: user.allowed_functions || [],
    });
    setError('');
    setShowModal(true);
    // Fetch the user's existing per-page grants so the modal can
    // populate the checkboxes. ADMINs return is_full_access=true
    // with the entire catalog, which we surface as "all checked".
    const res = await api.get(`/admin/users/${user.user_id}/permissions`);
    if (res?.ok) {
      const data = await res.json();
      setPagePermissions(data.page_keys || []);
    } else {
      setPagePermissions([]);
    }
  }

  function togglePagePermission(pageKey) {
    setPagePermissions((prev) => (
      prev.includes(pageKey)
        ? prev.filter((k) => k !== pageKey)
        : [...prev, pageKey]
    ));
  }

  function selectAllPagesInGroup(group) {
    setPagePermissions((prev) => {
      const next = new Set(prev);
      group.pages.forEach((p) => next.add(p.key));
      return Array.from(next);
    });
  }

  function clearPagesInGroup(group) {
    const groupKeys = new Set(group.pages.map((p) => p.key));
    setPagePermissions((prev) => prev.filter((k) => !groupKeys.has(k)));
  }

  function selectAllPages() {
    setPagePermissions([...ALL_PAGE_KEYS]);
  }

  function clearAllPages() {
    setPagePermissions([]);
  }

  async function save() {
    setError('');
    // UpdateUserRequest does not accept `username` (the user_id is in the
    // URL path, and V-017 extras=forbid rejects unknown fields). Only
    // include it on the create path where CreateUserRequest expects it.
    const body = {
      full_name: form.full_name,
      role: form.role,
      warehouse_ids: form.warehouse_ids || [],
      allowed_functions: form.allowed_functions || [],
    };
    if (!editId) {
      body.username = form.username;
      body.password = form.password;
    } else if (form.password) {
      body.password = form.password;
    }
    const res = editId
      ? await api.put(`/admin/users/${editId}`, body)
      : await api.post('/admin/users', body);
    if (!res?.ok) {
      const data = await res?.json();
      setError(data?.error || 'Failed to save');
      return;
    }
    // Persist per-page grants in a follow-up PUT. The endpoint is a
    // no-op for ADMIN targets (server bypasses the table), so calling
    // it unconditionally is safe. Resolve the user_id from the
    // create response when we just minted a new row.
    const userPayload = await res.json().catch(() => null);
    const savedUserId = editId || userPayload?.user_id;
    if (savedUserId) {
      const permRes = await api.put(
        `/admin/users/${savedUserId}/permissions`,
        { page_keys: pagePermissions },
      );
      if (!permRes?.ok) {
        const permData = await permRes?.json();
        setError(
          permData?.error || 'User saved, but failed to save page permissions',
        );
        return;
      }
    }
    setShowModal(false);
    loadUsers();
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

  function selectAllWarehouses() {
    setForm({
      ...form,
      warehouse_ids: warehouses.map((wh) => wh.warehouse_id),
    });
  }

  function clearWarehouses() {
    setForm({ ...form, warehouse_ids: [] });
  }

  function selectAllFunctions() {
    setForm({
      ...form,
      allowed_functions: ALL_FUNCTIONS.map((fn) => fn.key),
    });
  }

  function clearFunctions() {
    setForm({ ...form, allowed_functions: [] });
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
    { key: 'actions', label: '', render: (r) => (
      <div style={{ display: 'flex', gap: 4 }}>
        <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); openEdit(r); }} aria-label="Edit" title="Edit">&#9998;</button>
        {r.user_id !== currentUser?.user_id && (
          <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); deleteUser(r.user_id); }} aria-label="Delete" title="Delete">&#128465;</button>
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
          size="wide"
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
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <label>Warehouses</label>
              {warehouses.length > 0 && (
                <span style={{ fontSize: 12 }}>
                  <button
                    type="button"
                    onClick={selectAllWarehouses}
                    style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', padding: 0, fontSize: 12 }}
                  >
                    Select all
                  </button>
                  <span style={{ color: 'var(--text-tertiary)', margin: '0 6px' }}>/</span>
                  <button
                    type="button"
                    onClick={clearWarehouses}
                    style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', padding: 0, fontSize: 12 }}
                  >
                    Clear
                  </button>
                </span>
              )}
            </div>
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
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <label>Mobile Module Access</label>
              <span style={{ fontSize: 12 }}>
                <button
                  type="button"
                  onClick={selectAllFunctions}
                  style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', padding: 0, fontSize: 12 }}
                >
                  Select all
                </button>
                <span style={{ color: 'var(--text-tertiary)', margin: '0 6px' }}>/</span>
                <button
                  type="button"
                  onClick={clearFunctions}
                  style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', padding: 0, fontSize: 12 }}
                >
                  Clear
                </button>
              </span>
            </div>
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

          <div className="form-group">
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <label>Web Admin Page Access</label>
              <span style={{ fontSize: 12 }}>
                <button
                  type="button"
                  onClick={selectAllPages}
                  style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', padding: 0, fontSize: 12 }}
                >
                  Select all
                </button>
                <span style={{ color: 'var(--text-tertiary)', margin: '0 6px' }}>/</span>
                <button
                  type="button"
                  onClick={clearAllPages}
                  style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', padding: 0, fontSize: 12 }}
                >
                  Clear
                </button>
              </span>
            </div>
            {form.role === 'ADMIN' ? (
              <p style={{ fontSize: 12, color: 'var(--text-secondary)', padding: '8px 0' }}>
                ADMIN role bypasses page permissions - full access to every web admin page.
              </p>
            ) : (
              <div style={{
                display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
                gap: 12, padding: '8px 0',
              }}>
                {PAGE_GROUPS.map((group) => {
                  const groupKeys = new Set(group.pages.map((p) => p.key));
                  const grantedCount = pagePermissions.filter((k) => groupKeys.has(k)).length;
                  // The Overrides card carries an accent border so it
                  // reads as a grant of authority, not a page-access
                  // toggle. Granting an override is a different mental
                  // model from picking which pages a USER can open.
                  const cardStyle = group.isOverride
                    ? { padding: 10, borderLeft: '3px solid var(--copper)' }
                    : { padding: 10 };
                  return (
                    <div key={group.label} className="card" style={cardStyle}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                        <strong style={{ fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.5, color: group.isOverride ? 'var(--copper)' : undefined }}>
                          {group.label}
                        </strong>
                        <span style={{ fontSize: 11 }}>
                          <button
                            type="button"
                            onClick={() => selectAllPagesInGroup(group)}
                            style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', padding: 0, fontSize: 11 }}
                          >
                            All
                          </button>
                          <span style={{ color: 'var(--text-tertiary)', margin: '0 4px' }}>/</span>
                          <button
                            type="button"
                            onClick={() => clearPagesInGroup(group)}
                            style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', padding: 0, fontSize: 11 }}
                          >
                            None
                          </button>
                        </span>
                      </div>
                      {group.pages.map((p) => (
                        <label key={p.key} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0', cursor: 'pointer', fontSize: 13 }}>
                          <input
                            type="checkbox"
                            checked={pagePermissions.includes(p.key)}
                            onChange={() => togglePagePermission(p.key)}
                          />
                          {p.label}
                        </label>
                      ))}
                      {grantedCount > 0 && grantedCount < group.pages.length && (
                        <div style={{ fontSize: 10, color: 'var(--text-tertiary)', marginTop: 4 }}>
                          {grantedCount} / {group.pages.length}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
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
