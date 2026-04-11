import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

export default function Settings() {
  const { warehouseId } = useWarehouse();
  const [warehouse, setWarehouse] = useState(null);
  const [whForm, setWhForm] = useState({});
  const [editingWh, setEditingWh] = useState(false);
  const [showPO, setShowPO] = useState(false);
  const [showSO, setShowSO] = useState(false);
  const [poForm, setPoForm] = useState({ po_number: '', vendor_name: '', vendor_address: '', warehouse_id: null, lines: [{ item_id: '', quantity_expected: '' }] });
  const [soForm, setSoForm] = useState({ order_number: '', customer_name: '', address_line_1: '', address_line_2: '', city: '', state: '', zip: '', phone: '', warehouse_id: null, lines: [{ item_id: '', quantity: '' }] });
  const [formError, setFormError] = useState('');
  const [formSuccess, setFormSuccess] = useState('');

  // Settings with save button
  const [savedSettings, setSavedSettings] = useState({});
  const [draftSettings, setDraftSettings] = useState({});
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsError, setSettingsError] = useState('');
  const [settingsSuccess, setSettingsSuccess] = useState('');
  const [receivingBins, setReceivingBins] = useState([]);

  const hasUnsavedChanges = JSON.stringify(savedSettings) !== JSON.stringify(draftSettings);

  // Warn on browser navigation away with unsaved changes
  useEffect(() => {
    function handleBeforeUnload(e) {
      if (hasUnsavedChanges) {
        e.preventDefault();
        e.returnValue = '';
      }
    }
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [hasUnsavedChanges]);

  useEffect(() => {
    if (!warehouseId) return;
    api.get(`/admin/warehouses/${warehouseId}`).then(async (res) => {
      if (res?.ok) {
        const data = await res.json();
        setWarehouse(data);
        setWhForm(data);
      }
    });

    // Load all settings
    Promise.all([
      api.get('/admin/settings/count_show_expected'),
      api.get('/admin/settings/require_packing_before_shipping'),
      api.get('/admin/settings/allow_over_receiving'),
      api.get('/admin/settings/default_receiving_bin'),
    ]).then(async (responses) => {
      const initial = {};
      for (const res of responses) {
        if (res?.ok) {
          const data = await res.json();
          initial[data.key] = data.value;
        }
      }
      // Set defaults for missing settings
      if (!('count_show_expected' in initial)) initial.count_show_expected = 'true';
      if (!('require_packing_before_shipping' in initial)) initial.require_packing_before_shipping = 'true';
      if (!('allow_over_receiving' in initial)) initial.allow_over_receiving = 'true';
      if (!('default_receiving_bin' in initial)) initial.default_receiving_bin = '';
      setSavedSettings({ ...initial });
      setDraftSettings({ ...initial });
    });

    api.get(`/admin/bins?warehouse_id=${warehouseId}&bin_type=Staging`).then(async (res) => {
      if (res?.ok) {
        const data = await res.json();
        setReceivingBins(data.bins || []);
      }
    }).catch(() => {
      api.get(`/admin/bins?warehouse_id=${warehouseId}`).then(async (res) => {
        if (res?.ok) {
          const data = await res.json();
          setReceivingBins((data.bins || []).filter((b) => b.bin_type === 'Staging'));
        }
      }).catch(() => {});
    });
  }, [warehouseId]);

  function updateDraft(key, value) {
    setDraftSettings((prev) => ({ ...prev, [key]: value }));
    setSettingsSuccess('');
  }

  async function saveSettings() {
    setSettingsSaving(true);
    setSettingsError('');
    setSettingsSuccess('');
    const res = await api.put('/admin/settings', { settings: draftSettings });
    if (res?.ok) {
      setSavedSettings({ ...draftSettings });
      setSettingsSuccess('Settings saved');
    } else {
      const data = await res?.json();
      setSettingsError(data?.error || 'Failed to save settings');
    }
    setSettingsSaving(false);
  }

  async function saveWarehouse() {
    const res = await api.put(`/admin/warehouses/${warehouseId}`, { warehouse_name: whForm.warehouse_name, address: whForm.address });
    if (res?.ok) {
      setWarehouse(await res.json());
      setEditingWh(false);
    }
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
    const body = { ...poForm, lines: poForm.lines.filter((l) => l.item_id).map((l) => ({ item_id: Number(l.item_id), quantity_expected: Number(l.quantity_expected), quantity_ordered: Number(l.quantity_expected) })) };
    const res = await api.post('/admin/purchase-orders', body);
    if (res?.ok) {
      setFormSuccess('PO created');
      setShowPO(false);
      setPoForm({ po_number: '', vendor_name: '', vendor_address: '', warehouse_id: warehouseId, lines: [{ item_id: '', quantity_expected: '' }] });
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
    const shipAddress = [soForm.address_line_1, soForm.address_line_2, soForm.city, soForm.state, soForm.zip].filter(Boolean).join(', ');
    const body = {
      so_number: soForm.order_number,
      customer_name: soForm.customer_name,
      customer_phone: soForm.phone,
      customer_address: shipAddress,
      ship_address: shipAddress,
      warehouse_id: soForm.warehouse_id,
      lines: soForm.lines.filter((l) => l.item_id).map((l) => ({ item_id: Number(l.item_id), quantity: Number(l.quantity), quantity_ordered: Number(l.quantity) })),
    };
    const res = await api.post('/admin/sales-orders', body);
    if (res?.ok) {
      setFormSuccess('SO created');
      setShowSO(false);
      setSoForm({ order_number: '', customer_name: '', address_line_1: '', address_line_2: '', city: '', state: '', zip: '', phone: '', warehouse_id: warehouseId, lines: [{ item_id: '', quantity: '' }] });
    } else {
      const data = await res?.json();
      setFormError(data?.error || 'Failed to create SO');
    }
  }

  const toBool = (v) => v !== 'false' && v !== false;

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
              checked={toBool(draftSettings.require_packing_before_shipping)}
              onChange={(e) => updateDraft('require_packing_before_shipping', String(e.target.checked))}
            />
            Require packing before shipping
          </label>
        </div>
        <p className="settings-note">When enabled, orders must be packed before they can be shipped. When disabled, picked orders can be shipped directly.</p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0', marginTop: 8 }}>
          <label style={{ fontSize: 13, whiteSpace: 'nowrap' }}>Default Receiving Bin</label>
          <select
            className="form-select"
            style={{ width: 200 }}
            value={draftSettings.default_receiving_bin || ''}
            onChange={(e) => updateDraft('default_receiving_bin', e.target.value)}
          >
            <option value="">Select bin...</option>
            {receivingBins.map((b) => (
              <option key={b.bin_id} value={String(b.bin_id)}>{b.bin_code}</option>
            ))}
          </select>
        </div>
        <p className="settings-note">The default bin where received items are staged. Mobile users can override this per session.</p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0', marginTop: 8 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13 }}>
            <input
              type="checkbox"
              checked={toBool(draftSettings.allow_over_receiving)}
              onChange={(e) => updateDraft('allow_over_receiving', String(e.target.checked))}
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
              checked={toBool(draftSettings.count_show_expected)}
              onChange={(e) => updateDraft('count_show_expected', String(e.target.checked))}
            />
            Show expected quantities during cycle counts
          </label>
        </div>
        <p className="settings-note">When disabled, counters won't see expected quantities - useful for blind counts.</p>
      </div>

      {/* Save button */}
      <div className="settings-section" style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <button className="btn btn-primary" onClick={saveSettings} disabled={!hasUnsavedChanges || settingsSaving}>
          {settingsSaving ? 'Saving...' : 'Save Settings'}
        </button>
        {hasUnsavedChanges && <span style={{ fontSize: 12, color: 'var(--copper)' }}>Unsaved changes</span>}
        {settingsSuccess && <span style={{ fontSize: 12, color: 'var(--success)' }}>{settingsSuccess}</span>}
        {settingsError && <span style={{ fontSize: 12, color: 'var(--danger)' }}>{settingsError}</span>}
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
          <span className="detail-label">Version</span><span className="mono">0.9.8</span>
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
          <div className="form-group">
            <label>Vendor Address</label>
            <input className="form-input" value={poForm.vendor_address} onChange={(e) => setPoForm({ ...poForm, vendor_address: e.target.value })} placeholder="Optional" />
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
          <div className="form-group">
            <label>Address Line 1</label>
            <input className="form-input" value={soForm.address_line_1} onChange={(e) => setSoForm({ ...soForm, address_line_1: e.target.value })} />
          </div>
          <div className="form-group">
            <label>Address Line 2</label>
            <input className="form-input" value={soForm.address_line_2} onChange={(e) => setSoForm({ ...soForm, address_line_2: e.target.value })} />
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>City</label>
              <input className="form-input" value={soForm.city} onChange={(e) => setSoForm({ ...soForm, city: e.target.value })} />
            </div>
            <div className="form-group">
              <label>State</label>
              <input className="form-input" value={soForm.state} onChange={(e) => setSoForm({ ...soForm, state: e.target.value })} />
            </div>
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>Zip</label>
              <input className="form-input" value={soForm.zip} onChange={(e) => setSoForm({ ...soForm, zip: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Phone</label>
              <input className="form-input" value={soForm.phone} onChange={(e) => setSoForm({ ...soForm, phone: e.target.value })} />
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
