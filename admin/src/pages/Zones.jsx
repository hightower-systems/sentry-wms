import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

const ZONE_TYPES = ['RECEIVING', 'STORAGE', 'PICKING', 'STAGING', 'SHIPPING'];

export default function Zones() {
  const { warehouseId } = useWarehouse();
  const [zones, setZones] = useState([]);
  const [showModal, setShowModal] = useState(false);
  const [editId, setEditId] = useState(null);
  const [form, setForm] = useState({});
  const [error, setError] = useState('');
  const [deleteTarget, setDeleteTarget] = useState(null);

  useEffect(() => { if (warehouseId) loadZones(); }, [warehouseId]);

  async function loadZones() {
    const res = await api.get(`/admin/zones?warehouse_id=${warehouseId}`);
    if (res?.ok) {
      const data = await res.json();
      setZones(data.zones || []);
    }
  }

  function openCreate() {
    setEditId(null);
    setForm({ is_active: true });
    setError('');
    setShowModal(true);
  }

  function openEdit(zone) {
    setEditId(zone.zone_id);
    setForm(zone);
    setError('');
    setShowModal(true);
  }

  async function save() {
    setError('');
    const body = { zone_code: form.zone_code, zone_name: form.zone_name, zone_type: form.zone_type };
    const res = editId
      ? await api.put(`/admin/zones/${editId}`, { ...body, is_active: !!form.is_active })
      : await api.post('/admin/zones', { ...body, warehouse_id: warehouseId });
    if (res?.ok) {
      setShowModal(false);
      loadZones();
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to save');
    }
  }

  async function deleteZone() {
    setError('');
    const target = deleteTarget;
    if (!target) return;
    const res = await api.delete(`/admin/zones/${target.zone_id}`);
    if (res?.ok) {
      setDeleteTarget(null);
      loadZones();
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to delete zone');
      setDeleteTarget(null);
    }
  }

  const columns = [
    { key: 'zone_code', label: 'Zone Code', mono: true },
    { key: 'zone_name', label: 'Zone Name' },
    { key: 'zone_type', label: 'Type' },
    { key: 'is_active', label: 'Active', render: (r) => r.is_active ? 'Yes' : 'No' },
    { key: 'actions', label: '', render: (r) => (
      <div style={{ display: 'flex', gap: 4 }}>
        <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); openEdit(r); }} aria-label="Edit" title="Edit">&#9998;</button>
        <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); setDeleteTarget(r); }} aria-label="Delete" title="Delete">&#128465;</button>
      </div>
    )},
  ];

  return (
    <div>
      <PageHeader title="Zones">
        <button className="btn btn-primary" onClick={openCreate}>New Zone</button>
      </PageHeader>
      <DataTable columns={columns} data={zones} emptyMessage="No zones found" />

      {showModal && (
        <Modal
          title={editId ? 'Edit Zone' : 'New Zone'}
          onClose={() => { setShowModal(false); setError(''); }}
          footer={
            <>
              <button className="btn" onClick={() => { setShowModal(false); setError(''); }}>Cancel</button>
              <button className="btn btn-primary" onClick={save}>Save</button>
            </>
          }
        >
          {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
          <div className="form-group">
            <label>Zone Code</label>
            <input className="form-input" value={form.zone_code || ''} onChange={(e) => setForm({ ...form, zone_code: e.target.value })} />
          </div>
          <div className="form-group">
            <label>Zone Name</label>
            <input className="form-input" value={form.zone_name || ''} onChange={(e) => setForm({ ...form, zone_name: e.target.value })} />
          </div>
          <div className="form-group">
            <label>Type</label>
            <select className="form-select" value={form.zone_type || ''} onChange={(e) => setForm({ ...form, zone_type: e.target.value })}>
              <option value="">Select type</option>
              {ZONE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
        </Modal>
      )}

      {deleteTarget && (
        <Modal
          title={`Delete zone ${deleteTarget.zone_code || ''}?`}
          onClose={() => setDeleteTarget(null)}
          footer={
            <>
              <button className="btn" onClick={() => setDeleteTarget(null)}>Cancel</button>
              <button className="btn btn-danger" onClick={deleteZone}>Delete</button>
            </>
          }
        >
          <p style={{ fontSize: 13 }}>
            This permanently removes the zone. Bins assigned to it must be
            reassigned or deleted first.
          </p>
          {error && <div className="form-error" style={{ marginTop: 12 }}>{error}</div>}
        </Modal>
      )}
    </div>
  );
}
