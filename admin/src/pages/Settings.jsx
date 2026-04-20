import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import { useDirtyFormGuard } from '../hooks/useDirtyFormGuard.js';

export default function Settings() {
  const { warehouseId } = useWarehouse();
  const [warehouse, setWarehouse] = useState(null);
  const [whForm, setWhForm] = useState({});
  const [editingWh, setEditingWh] = useState(false);
  const [showPO, setShowPO] = useState(false);
  const [showSO, setShowSO] = useState(false);
  const [poForm, setPoForm] = useState({ po_number: '', vendor_name: '', vendor_address: '', warehouse_id: null, lines: [{ sku: '', quantity_ordered: '' }] });
  const [soForm, setSoForm] = useState({ order_number: '', customer_name: '', address_line_1: '', address_line_2: '', city: '', state: '', zip: '', phone: '', warehouse_id: null, lines: [{ sku: '', quantity_ordered: '' }] });
  const [itemsBySku, setItemsBySku] = useState(new Map());
  const [itemsLoaded, setItemsLoaded] = useState(false);
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

  // v1.4.2 #100: browser-level warning only. Hook owns the
  // beforeunload listener. Intra-SPA sidebar clicks are NOT guarded;
  // deferred to v1.5 along with the rest of the design question.
  useDirtyFormGuard(hasUnsavedChanges);

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
      api.get('/admin/settings/require_count_approval_separation'),
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
      if (!('require_count_approval_separation' in initial)) initial.require_count_approval_separation = 'false';
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

  // v1.4.2 #93: cache SKU -> item_id lookup so the PO/SO manual-entry
  // lines can accept an SKU (what operators memorize) instead of a
  // raw item_id (a database autoincrement). Loaded lazily when either
  // modal opens so Settings' first paint does not fetch /admin/items.
  async function ensureItemsLoaded() {
    if (itemsLoaded) return;
    const res = await api.get('/admin/items?per_page=1000&active=true');
    if (res?.ok) {
      const data = await res.json();
      const map = new Map();
      for (const it of data.items || []) {
        if (it.sku) map.set(String(it.sku).trim().toLowerCase(), it.item_id);
      }
      setItemsBySku(map);
      setItemsLoaded(true);
    }
  }

  function resolveSku(sku) {
    return itemsBySku.get(String(sku || '').trim().toLowerCase());
  }

  function openPoModal() {
    setShowPO(true);
    setFormError('');
    ensureItemsLoaded();
  }

  function openSoModal() {
    setShowSO(true);
    setFormError('');
    ensureItemsLoaded();
  }

  // v1.4.2 #92: reset modal state on every Cancel/close. Without this,
  // a second "Create PO" click reopens the modal with stale fields
  // from the previous attempt -- either data that was already
  // submitted or a half-filled form the user abandoned.
  function closePoModal() {
    setShowPO(false);
    setPoForm({ po_number: '', vendor_name: '', vendor_address: '', warehouse_id: warehouseId, lines: [{ sku: '', quantity_ordered: '' }] });
    setFormError('');
  }

  function closeSoModal() {
    setShowSO(false);
    setSoForm({ order_number: '', customer_name: '', address_line_1: '', address_line_2: '', city: '', state: '', zip: '', phone: '', warehouse_id: warehouseId, lines: [{ sku: '', quantity_ordered: '' }] });
    setFormError('');
  }

  // PO lines
  function addPOLine() { setPoForm({ ...poForm, lines: [...poForm.lines, { sku: '', quantity_ordered: '' }] }); }
  function updatePOLine(i, key, val) {
    const lines = [...poForm.lines];
    lines[i] = { ...lines[i], [key]: val };
    setPoForm({ ...poForm, lines });
  }

  async function createPO() {
    setFormError(''); setFormSuccess('');
    const resolved = [];
    for (const l of poForm.lines.filter((x) => x.sku)) {
      const itemId = resolveSku(l.sku);
      if (!itemId) {
        setFormError(`Unknown SKU: ${l.sku}`);
        return;
      }
      resolved.push({ item_id: itemId, quantity_ordered: Number(l.quantity_ordered) });
    }
    const body = {
      po_number: poForm.po_number,
      warehouse_id: poForm.warehouse_id || warehouseId,
      vendor_name: poForm.vendor_name || null,
      notes: poForm.vendor_address ? `Vendor address: ${poForm.vendor_address}` : null,
      lines: resolved,
    };
    const res = await api.post('/admin/purchase-orders', body);
    if (res?.ok) {
      setFormSuccess('PO created');
      setShowPO(false);
      setPoForm({ po_number: '', vendor_name: '', vendor_address: '', warehouse_id: warehouseId, lines: [{ sku: '', quantity_ordered: '' }] });
    } else {
      const data = await res?.json();
      setFormError(data?.error || 'Failed to create PO');
    }
  }

  // SO lines
  function addSOLine() { setSoForm({ ...soForm, lines: [...soForm.lines, { sku: '', quantity_ordered: '' }] }); }
  function updateSOLine(i, key, val) {
    const lines = [...soForm.lines];
    lines[i] = { ...lines[i], [key]: val };
    setSoForm({ ...soForm, lines });
  }

  async function createSO() {
    setFormError(''); setFormSuccess('');
    const shipAddress = [soForm.address_line_1, soForm.address_line_2, soForm.city, soForm.state, soForm.zip].filter(Boolean).join(', ');
    const resolved = [];
    for (const l of soForm.lines.filter((x) => x.sku)) {
      const itemId = resolveSku(l.sku);
      if (!itemId) {
        setFormError(`Unknown SKU: ${l.sku}`);
        return;
      }
      resolved.push({ item_id: itemId, quantity_ordered: Number(l.quantity_ordered) });
    }
    const body = {
      so_number: soForm.order_number,
      customer_name: soForm.customer_name || null,
      customer_phone: soForm.phone || null,
      customer_address: shipAddress || null,
      ship_address: shipAddress || null,
      warehouse_id: soForm.warehouse_id || warehouseId,
      lines: resolved,
    };
    const res = await api.post('/admin/sales-orders', body);
    if (res?.ok) {
      setFormSuccess('SO created');
      setShowSO(false);
      setSoForm({ order_number: '', customer_name: '', address_line_1: '', address_line_2: '', city: '', state: '', zip: '', phone: '', warehouse_id: warehouseId, lines: [{ sku: '', quantity_ordered: '' }] });
    } else {
      const data = await res?.json();
      setFormError(data?.error || 'Failed to create SO');
    }
  }

  const toBool = (v) => v !== 'false' && v !== false;

  return (
    <div>
      {/* v1.4.2 #93: shared SKU datalist for the PO + SO manual-entry
          line inputs. Lives outside the modals so the <input list>
          reference resolves whether either modal is open. */}
      <datalist id="settings-sku-datalist">
        {Array.from(itemsBySku.keys()).map((k) => (
          <option key={k} value={k.toUpperCase()} />
        ))}
      </datalist>
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
          <button className="btn" onClick={openPoModal}>Create Purchase Order</button>
          <button className="btn" onClick={openSoModal}>Create Sales Order</button>
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

      {/* Inventory */}
      <div className="settings-section">
        <h3>Inventory</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13 }}>
            <input
              type="checkbox"
              checked={toBool(draftSettings.require_count_approval_separation)}
              onChange={(e) => updateDraft('require_count_approval_separation', String(e.target.checked))}
            />
            Require separate approver for cycle count adjustments
          </label>
        </div>
        <p className="settings-note">When enabled, the admin who performed a cycle count cannot approve the resulting adjustments. A different admin must review and approve.</p>
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

      {/* About */}
      <div className="settings-section">
        <h3>About</h3>
        <div className="detail-grid">
          <span className="detail-label">Version</span><span className="mono">1.4.2</span>
          <span className="detail-label">Repository</span><span><a href="https://github.com/hightower-systems/sentry-wms" target="_blank" rel="noopener noreferrer">github.com/hightower-systems/sentry-wms</a></span>
        </div>
      </div>

      {/* PO Modal */}
      {showPO && (
        <Modal title="Create Purchase Order" onClose={closePoModal}
          footer={<><button className="btn" onClick={closePoModal}>Cancel</button><button className="btn btn-primary" onClick={createPO}>Create PO</button></>}
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
                  <input className="form-input" list="settings-sku-datalist" placeholder="SKU" value={line.sku} onChange={(e) => updatePOLine(i, 'sku', e.target.value)} />
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <input className="form-input" type="number" placeholder="Qty ordered" value={line.quantity_ordered} onChange={(e) => updatePOLine(i, 'quantity_ordered', e.target.value)} />
                </div>
              </div>
            ))}
          </div>
        </Modal>
      )}

      {/* SO Modal */}
      {showSO && (
        <Modal title="Create Sales Order" onClose={closeSoModal}
          footer={<><button className="btn" onClick={closeSoModal}>Cancel</button><button className="btn btn-primary" onClick={createSO}>Create SO</button></>}
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
                  <input className="form-input" list="settings-sku-datalist" placeholder="SKU" value={line.sku} onChange={(e) => updateSOLine(i, 'sku', e.target.value)} />
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <input className="form-input" type="number" placeholder="Quantity" value={line.quantity_ordered} onChange={(e) => updateSOLine(i, 'quantity_ordered', e.target.value)} />
                </div>
              </div>
            ))}
          </div>
        </Modal>
      )}

    </div>
  );
}
