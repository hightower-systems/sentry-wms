import { useState, useEffect, useRef } from 'react';
import { api } from '../api.js';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

export default function Settings() {
  const [warehouse, setWarehouse] = useState(null);
  const [whForm, setWhForm] = useState({});
  const [editingWh, setEditingWh] = useState(false);
  const [importType, setImportType] = useState('items');
  const [importResult, setImportResult] = useState(null);
  const [showPO, setShowPO] = useState(false);
  const [showSO, setShowSO] = useState(false);
  const [poForm, setPoForm] = useState({ po_number: '', vendor_name: '', warehouse_id: 1, lines: [{ item_id: '', quantity_expected: '' }] });
  const [soForm, setSoForm] = useState({ order_number: '', customer_name: '', warehouse_id: 1, lines: [{ item_id: '', quantity: '' }] });
  const [formError, setFormError] = useState('');
  const [formSuccess, setFormSuccess] = useState('');
  const [countShowExpected, setCountShowExpected] = useState(true);
  const [requirePacking, setRequirePacking] = useState(true);
  const [packingError, setPackingError] = useState('');
  const [defaultReceivingBin, setDefaultReceivingBin] = useState('');
  const [receivingBins, setReceivingBins] = useState([]);
  const [allowOverReceiving, setAllowOverReceiving] = useState(true);
  const fileRef = useRef(null);

  useEffect(() => {
    api.get('/admin/warehouses/1').then(async (res) => {
      if (res?.ok) {
        const data = await res.json();
        setWarehouse(data);
        setWhForm(data);
      }
    });
    api.get('/admin/settings/count_show_expected').then(async (res) => {
      if (res?.ok) {
        const data = await res.json();
        setCountShowExpected(data.value !== 'false' && data.value !== false);
      }
    }).catch(() => {});
    api.get('/admin/settings/require_packing_before_shipping').then(async (res) => {
      if (res?.ok) {
        const data = await res.json();
        setRequirePacking(data.value !== 'false' && data.value !== false);
      }
    }).catch(() => {});
    api.get('/admin/settings/allow_over_receiving').then(async (res) => {
      if (res?.ok) {
        const data = await res.json();
        setAllowOverReceiving(data.value !== 'false' && data.value !== false);
      }
    }).catch(() => {});
    api.get('/admin/settings/default_receiving_bin').then(async (res) => {
      if (res?.ok) {
        const data = await res.json();
        setDefaultReceivingBin(data.value || '');
      }
    }).catch(() => {});
    api.get('/admin/bins?warehouse_id=1&bin_type=Staging').then(async (res) => {
      if (res?.ok) {
        const data = await res.json();
        setReceivingBins(data.bins || []);
      }
    }).catch(() => {
      // Fall back to all bins if filter not supported
      api.get('/admin/bins?warehouse_id=1').then(async (res) => {
        if (res?.ok) {
          const data = await res.json();
          setReceivingBins((data.bins || []).filter((b) => b.bin_type === 'Staging'));
        }
      }).catch(() => {});
    });
  }, []);

  async function saveWarehouse() {
    const res = await api.put('/admin/warehouses/1', { warehouse_name: whForm.warehouse_name, address: whForm.address });
    if (res?.ok) {
      setWarehouse(await res.json());
      setEditingWh(false);
    }
  }

  async function handleImport() {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setImportResult(null);

    const text = await file.text();
    let rows;

    if (file.name.endsWith('.json')) {
      rows = JSON.parse(text);
    } else {
      const lines = text.trim().split('\n');
      const headers = lines[0].split(',').map((h) => h.trim().replace(/^"|"$/g, ''));
      rows = lines.slice(1).map((line) => {
        const vals = line.split(',').map((v) => v.trim().replace(/^"|"$/g, ''));
        const obj = {};
        headers.forEach((h, i) => { obj[h] = vals[i] || ''; });
        return obj;
      });
    }

    const res = await api.post(`/admin/import/${importType}`, { [importType]: rows });
    if (res?.ok) {
      const data = await res.json();
      setImportResult(data);
    } else {
      const data = await res?.json();
      setImportResult({ error: data?.error || 'Import failed' });
    }
    fileRef.current.value = '';
  }

  // PO lines
  function addPOLine() { setPoForm({ ...poForm, lines: [...poForm.lines, { item_id: '', quantity_expected: '' }] }); }
  function updatePOLine(i, key, val) {
    const lines = [...poForm.lines];
    lines[i] = { ...lines[i], [key]: val };
    setPoForm({ ...poForm, lines });
  }

  async function createPO() {
    setFormError(''); setFormSuccess('');
    const body = { ...poForm, lines: poForm.lines.filter((l) => l.item_id).map((l) => ({ item_id: Number(l.item_id), quantity_expected: Number(l.quantity_expected) })) };
    const res = await api.post('/admin/purchase-orders', body);
    if (res?.ok) {
      setFormSuccess('PO created');
      setShowPO(false);
      setPoForm({ po_number: '', vendor_name: '', warehouse_id: 1, lines: [{ item_id: '', quantity_expected: '' }] });
    } else {
      const data = await res?.json();
      setFormError(data?.error || 'Failed to create PO');
    }
  }

  // SO lines
  function addSOLine() { setSoForm({ ...soForm, lines: [...soForm.lines, { item_id: '', quantity: '' }] }); }
  function updateSOLine(i, key, val) {
    const lines = [...soForm.lines];
    lines[i] = { ...lines[i], [key]: val };
    setSoForm({ ...soForm, lines });
  }

  async function createSO() {
    setFormError(''); setFormSuccess('');
    const body = { ...soForm, lines: soForm.lines.filter((l) => l.item_id).map((l) => ({ item_id: Number(l.item_id), quantity: Number(l.quantity) })) };
    const res = await api.post('/admin/sales-orders', body);
    if (res?.ok) {
      setFormSuccess('SO created');
      setShowSO(false);
      setSoForm({ order_number: '', customer_name: '', warehouse_id: 1, lines: [{ item_id: '', quantity: '' }] });
    } else {
      const data = await res?.json();
      setFormError(data?.error || 'Failed to create SO');
    }
  }

  return (
    <div>
      <PageHeader title="Settings" />

      {formSuccess && <div style={{ marginBottom: 12, padding: '8px 12px', background: 'var(--success-bg)', color: 'var(--success)', borderRadius: 'var(--radius)', fontSize: 13 }}>{formSuccess}</div>}

      {/* Warehouse config */}
      <div className="settings-section">
        <h3>Warehouse</h3>
        {warehouse && !editingWh && (
          <div>
            <div className="detail-grid" style={{ marginBottom: 12 }}>
              <span className="detail-label">Name</span><span>{warehouse.warehouse_name}</span>
              <span className="detail-label">Code</span><span className="mono">{warehouse.warehouse_code}</span>
              <span className="detail-label">Address</span><span>{warehouse.address || '-'}</span>
            </div>
            <button className="btn btn-sm" onClick={() => setEditingWh(true)}>Edit</button>
          </div>
        )}
        {editingWh && (
          <div>
            <div className="form-group">
              <label>Name</label>
              <input className="form-input" value={whForm.warehouse_name || ''} onChange={(e) => setWhForm({ ...whForm, warehouse_name: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Address</label>
              <input className="form-input" value={whForm.address || ''} onChange={(e) => setWhForm({ ...whForm, address: e.target.value })} />
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn" onClick={() => setEditingWh(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={saveWarehouse}>Save</button>
            </div>
          </div>
        )}
      </div>

      {/* Import tools */}
      <div className="settings-section">
        <h3>Import Tools</h3>
        <p className="settings-note">Upload a CSV or JSON file to bulk import items or bins.</p>
        <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
          <select className="form-select" style={{ width: 120 }} value={importType} onChange={(e) => setImportType(e.target.value)}>
            <option value="items">Items</option>
            <option value="bins">Bins</option>
          </select>
          <input ref={fileRef} type="file" accept=".csv,.json" style={{ fontSize: 13 }} />
          <button className="btn" onClick={handleImport}>Import</button>
        </div>
        {importResult && (
          <div className="import-results">
            {importResult.error ? (
              <div className="errors">{importResult.error}</div>
            ) : (
              <>
                <div className="success">Created: {importResult.created ?? 0}</div>
                {importResult.errors?.length > 0 && (
                  <div className="errors" style={{ marginTop: 4 }}>
                    Errors: {importResult.errors.length}
                    <ul style={{ margin: '4px 0 0 16px', fontSize: 12 }}>
                      {importResult.errors.slice(0, 10).map((err, i) => (
                        <li key={i}>Row {err.row}: {err.error}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {/* Manual PO/SO */}
      <div className="settings-section">
        <h3>Manual Entry</h3>
        <p className="settings-note">For standalone deployments or testing only. In production, POs and SOs come from your ERP.</p>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn" onClick={() => { setShowPO(true); setFormError(''); }}>Create Purchase Order</button>
          <button className="btn" onClick={() => { setShowSO(true); setFormError(''); }}>Create Sales Order</button>
        </div>
      </div>

      {/* Fulfillment Workflow */}
      <div className="settings-section">
        <h3>Fulfillment Workflow</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13 }}>
            <input
              type="checkbox"
              checked={requirePacking}
              onChange={async (e) => {
                const val = e.target.checked;
                setPackingError('');
                const res = await api.put('/admin/settings', { settings: { require_packing_before_shipping: String(val) } });
                if (res?.ok) {
                  setRequirePacking(val);
                } else {
                  const data = await res?.json();
                  setPackingError(data?.error || 'Failed to update setting');
                }
              }}
            />
            Require packing before shipping
          </label>
        </div>
        {packingError && <div className="form-error" style={{ marginTop: 4, fontSize: 13 }}>{packingError}</div>}
        <p className="settings-note">When enabled, orders must be packed before they can be shipped. When disabled, picked orders can be shipped directly.</p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0', marginTop: 8 }}>
          <label style={{ fontSize: 13, whiteSpace: 'nowrap' }}>Default Receiving Bin</label>
          <select
            className="form-select"
            style={{ width: 200 }}
            value={defaultReceivingBin}
            onChange={async (e) => {
              const val = e.target.value;
              setDefaultReceivingBin(val);
              await api.put('/admin/settings', { settings: { default_receiving_bin: val } });
            }}
          >
            <option value="">Select bin...</option>
            {receivingBins.map((b) => (
              <option key={b.id} value={String(b.id)}>{b.bin_code}</option>
            ))}
          </select>
        </div>
        <p className="settings-note">The default bin where received items are staged. Mobile users can override this per session.</p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0', marginTop: 8 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13 }}>
            <input
              type="checkbox"
              checked={allowOverReceiving}
              onChange={async (e) => {
                const val = e.target.checked;
                setAllowOverReceiving(val);
                await api.put('/admin/settings', { settings: { allow_over_receiving: String(val) } });
              }}
            />
            Allow over-receiving
          </label>
        </div>
        <p className="settings-note">When enabled, users can receive more than the PO quantity (with a warning). When disabled, over-receiving is blocked.</p>
      </div>

      {/* Mobile App Settings */}
      <div className="settings-section">
        <h3>Mobile App</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13 }}>
            <input
              type="checkbox"
              checked={countShowExpected}
              onChange={async (e) => {
                const val = e.target.checked;
                setCountShowExpected(val);
                await api.put('/admin/settings', { settings: { count_show_expected: String(val) } });
              }}
            />
            Show expected quantities during cycle counts
          </label>
        </div>
        <p className="settings-note">When disabled, counters won't see expected quantities - useful for blind counts.</p>
      </div>

      {/* Connector config placeholder */}
      <div className="settings-section">
        <h3>ERP Connectors</h3>
        <div className="placeholder-section">
          ERP connector settings coming in a future release.
        </div>
      </div>

      {/* About */}
      <div className="settings-section">
        <h3>About</h3>
        <div className="detail-grid">
          <span className="detail-label">Version</span><span className="mono">0.8.0</span>
          <span className="detail-label">Repository</span><span><a href="https://github.com/hightower-systems/sentry-wms" target="_blank" rel="noopener noreferrer">github.com/hightower-systems/sentry-wms</a></span>
        </div>
      </div>

      {/* PO Modal */}
      {showPO && (
        <Modal title="Create Purchase Order" onClose={() => setShowPO(false)}
          footer={<><button className="btn" onClick={() => setShowPO(false)}>Cancel</button><button className="btn btn-primary" onClick={createPO}>Create PO</button></>}
        >
          {formError && <div className="form-error" style={{ marginBottom: 12 }}>{formError}</div>}
          <div className="form-row">
            <div className="form-group">
              <label>PO Number</label>
              <input className="form-input" value={poForm.po_number} onChange={(e) => setPoForm({ ...poForm, po_number: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Vendor</label>
              <input className="form-input" value={poForm.vendor_name} onChange={(e) => setPoForm({ ...poForm, vendor_name: e.target.value })} />
            </div>
          </div>
          <div style={{ marginTop: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)' }}>Lines</label>
              <button className="btn btn-sm" onClick={addPOLine}>+ Line</button>
            </div>
            {poForm.lines.map((line, i) => (
              <div className="form-row" key={i} style={{ marginBottom: 8 }}>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <input className="form-input" type="number" placeholder="Item ID" value={line.item_id} onChange={(e) => updatePOLine(i, 'item_id', e.target.value)} />
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <input className="form-input" type="number" placeholder="Qty expected" value={line.quantity_expected} onChange={(e) => updatePOLine(i, 'quantity_expected', e.target.value)} />
                </div>
              </div>
            ))}
          </div>
        </Modal>
      )}

      {/* SO Modal */}
      {showSO && (
        <Modal title="Create Sales Order" onClose={() => setShowSO(false)}
          footer={<><button className="btn" onClick={() => setShowSO(false)}>Cancel</button><button className="btn btn-primary" onClick={createSO}>Create SO</button></>}
        >
          {formError && <div className="form-error" style={{ marginBottom: 12 }}>{formError}</div>}
          <div className="form-row">
            <div className="form-group">
              <label>SO Number</label>
              <input className="form-input" value={soForm.order_number} onChange={(e) => setSoForm({ ...soForm, order_number: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Customer</label>
              <input className="form-input" value={soForm.customer_name} onChange={(e) => setSoForm({ ...soForm, customer_name: e.target.value })} />
            </div>
          </div>
          <div style={{ marginTop: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)' }}>Lines</label>
              <button className="btn btn-sm" onClick={addSOLine}>+ Line</button>
            </div>
            {soForm.lines.map((line, i) => (
              <div className="form-row" key={i} style={{ marginBottom: 8 }}>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <input className="form-input" type="number" placeholder="Item ID" value={line.item_id} onChange={(e) => updateSOLine(i, 'item_id', e.target.value)} />
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <input className="form-input" type="number" placeholder="Quantity" value={line.quantity} onChange={(e) => updateSOLine(i, 'quantity', e.target.value)} />
                </div>
              </div>
            ))}
          </div>
        </Modal>
      )}
    </div>
  );
}
