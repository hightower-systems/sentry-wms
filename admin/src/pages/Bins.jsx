import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

const BIN_TYPES = ['Staging', 'PickableStaging', 'Pickable'];

export default function Bins() {
  const { warehouseId } = useWarehouse();
  const [bins, setBins] = useState([]);
  const [zones, setZones] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({});
  const [error, setError] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => { if (warehouseId) { loadBins(); loadZones(); } }, [warehouseId]);

  async function loadBins() {
    const res = await api.get(`/admin/bins?warehouse_id=${warehouseId}`);
    if (res?.ok) {
      const data = await res.json();
      setBins(data.bins || []);
    }
  }

  async function loadZones() {
    const res = await api.get(`/admin/zones?warehouse_id=${warehouseId}`);
    if (res?.ok) {
      const data = await res.json();
      setZones(data.zones || []);
    }
  }

  async function viewBin(bin) {
    setSelected(bin);
    setEditing(false);
    const res = await api.get(`/admin/bins/${bin.bin_id}`);
    if (res?.ok) {
      const data = await res.json();
      const flat = { ...(data.bin || {}), inventory: data.inventory || [] };
      setDetail(flat);
      setForm(flat);
    }
  }

  async function deleteBin() {
    setError('');
    const res = await api.delete(`/admin/bins/${selected.bin_id}`);
    if (res?.ok) {
      setConfirmDelete(false);
      setSelected(null);
      setDetail(null);
      loadBins();
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to delete');
      setConfirmDelete(false);
    }
  }

  async function saveBin() {
    setError('');
    const body = {
      bin_code: form.bin_code,
      bin_barcode: form.bin_barcode,
      bin_type: form.bin_type,
      zone_id: form.zone_id ? Number(form.zone_id) : null,
      aisle: form.aisle || null,
      pick_sequence: form.pick_sequence !== '' && form.pick_sequence != null ? Number(form.pick_sequence) : 0,
    };
    const res = editing
      ? await api.put(`/admin/bins/${selected.bin_id}`, { ...body, is_active: !!form.is_active })
      : await api.post('/admin/bins', { ...body, warehouse_id: warehouseId });
    if (res?.ok) {
      setSelected(null); setDetail(null); setShowCreate(false); setEditing(false);
      loadBins();
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to save');
    }
  }

  const columns = [
    { key: 'bin_code', label: 'Bin Code', mono: true },
    { key: 'bin_barcode', label: 'Barcode', mono: true },
    { key: 'bin_type', label: 'Type' },
    { key: 'zone_name', label: 'Zone' },
    { key: 'aisle', label: 'Aisle' },
    { key: 'pick_sequence', label: 'Pick Seq' },
    { key: 'is_active', label: 'Active', render: (r) => r.is_active ? 'Yes' : 'No' },
  ];

  const invCols = [
    { key: 'sku', label: 'SKU', mono: true },
    { key: 'item_name', label: 'Item' },
    { key: 'quantity_on_hand', label: 'On Hand' },
    { key: 'quantity_allocated', label: 'Allocated' },
  ];

  function renderForm() {
    return (
      <>
        {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
        <div className="form-row">
          <div className="form-group">
            <label>Bin Code</label>
            <input className="form-input" value={form.bin_code || ''} onChange={(e) => setForm({ ...form, bin_code: e.target.value })} />
          </div>
          <div className="form-group">
            <label>Barcode</label>
            <input className="form-input" value={form.bin_barcode || ''} onChange={(e) => setForm({ ...form, bin_barcode: e.target.value })} />
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Type</label>
            <select className="form-select" value={form.bin_type || ''} onChange={(e) => setForm({ ...form, bin_type: e.target.value })}>
              <option value="">Select type</option>
              {BIN_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div className="form-group">
            <label>Zone</label>
            <select className="form-select" value={form.zone_id || ''} onChange={(e) => setForm({ ...form, zone_id: Number(e.target.value) })}>
              <option value="">Select zone</option>
              {zones.map((z) => <option key={z.zone_id} value={z.zone_id}>{z.zone_code} - {z.zone_name}</option>)}
            </select>
          </div>
        </div>
        <div className="form-row">
          <div className="form-group">
            <label>Aisle</label>
            <input className="form-input" value={form.aisle || ''} onChange={(e) => setForm({ ...form, aisle: e.target.value })} />
          </div>
          <div className="form-group">
            <label>Pick Sequence</label>
            <input className="form-input" type="number" value={form.pick_sequence ?? ''} onChange={(e) => setForm({ ...form, pick_sequence: e.target.value })} />
          </div>
        </div>
      </>
    );
  }

  return (
    <div>
      <PageHeader title="Bins">
        <button className="btn btn-primary" onClick={() => { setForm({ is_active: true }); setShowCreate(true); setError(''); }}>New Bin</button>
      </PageHeader>
      <DataTable columns={columns} data={bins} onRowClick={viewBin} />

      {selected && detail && !editing && (
        <Modal title={`Bin ${detail.bin_code}`} onClose={() => { setSelected(null); setDetail(null); setConfirmDelete(false); setError(''); }}
          footer={
            <>
              <button className="btn btn-danger" onClick={() => setConfirmDelete(true)}>Delete</button>
              <button className="btn" onClick={() => { setEditing(true); setForm(detail); setError(''); }}>Edit</button>
            </>
          }
        >
          <div className="detail-grid">
            <span className="detail-label">Code</span><span className="mono">{detail.bin_code}</span>
            <span className="detail-label">Barcode</span><span className="mono">{detail.bin_barcode}</span>
            <span className="detail-label">Type</span><span>{detail.bin_type}</span>
            <span className="detail-label">Zone</span><span>{detail.zone_name || '-'}</span>
            <span className="detail-label">Aisle</span><span>{detail.aisle || '-'}</span>
            <span className="detail-label">Pick Seq</span><span>{detail.pick_sequence ?? '-'}</span>
            <span className="detail-label">Active</span><span>{detail.is_active ? 'Yes' : 'No'}</span>
          </div>
          {detail.inventory && detail.inventory.length > 0 && (
            <>
              <div className="section-title">Inventory</div>
              <DataTable columns={invCols} data={detail.inventory} />
            </>
          )}
          {error && <div className="form-error" style={{ marginTop: 12 }}>{error}</div>}
        </Modal>
      )}

      {confirmDelete && detail && (
        <Modal
          title={`Delete bin ${detail.bin_code}?`}
          onClose={() => setConfirmDelete(false)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmDelete(false)}>Cancel</button>
              <button className="btn btn-danger" onClick={deleteBin}>Delete</button>
            </>
          }
        >
          <p style={{ fontSize: 13 }}>
            This permanently removes bin <span className="mono">{detail.bin_code}</span>. Inventory with
            quantity on hand and preferred-bin references must be cleared first.
          </p>
        </Modal>
      )}

      {(editing || showCreate) && (
        <Modal
          title={editing ? `Edit Bin ${form.bin_code}` : 'New Bin'}
          onClose={() => { setEditing(false); setShowCreate(false); setSelected(null); setDetail(null); }}
          footer={
            <>
              <button className="btn" onClick={() => { setEditing(false); setShowCreate(false); setSelected(null); setDetail(null); }}>Cancel</button>
              <button className="btn btn-primary" onClick={saveBin}>Save</button>
            </>
          }
        >
          {renderForm()}
        </Modal>
      )}
    </div>
  );
}
