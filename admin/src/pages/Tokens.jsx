import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

// Rotation badges computed server-side, rendered client-side.
const ROTATION_BADGE = {
  none: null,
  recommended: { label: 'rotation recommended', color: '#c49100' },
  overdue: { label: 'rotation overdue', color: 'var(--danger)' },
};

const STATUS_BADGE = {
  active: { label: 'active', color: 'var(--text-secondary)' },
  revoked: { label: 'revoked', color: 'var(--danger)' },
  expired: { label: 'expired', color: 'var(--danger)' },
};

function Badge({ label, color }) {
  return (
    <span style={{
      display: 'inline-block',
      padding: '1px 8px',
      borderRadius: 10,
      fontSize: 11,
      fontWeight: 600,
      color: '#fff',
      background: color,
    }}>
      {label}
    </span>
  );
}

function renderCsv(list) {
  if (!list || list.length === 0) return <span style={{ color: 'var(--text-secondary)' }}>—</span>;
  return <span className="mono" style={{ fontSize: 12 }}>{list.join(', ')}</span>;
}

// #159: reusable checkbox picker used by the three token-scope
// fields on the create modal. `options` is the pool the admin can
// pick from (from /admin/scope-catalog or /admin/warehouses);
// `value` is the currently-selected array; `onChange` receives the
// new array. "All" / "None" buttons are inline so the common case
// (grant everything / deny everything) is a single click.
function ScopeCheckboxList({ options, value, onChange, renderLabel, keyOf }) {
  const selected = new Set(value);
  const allKeys = options.map(keyOf);
  const allSelected = allKeys.length > 0 && allKeys.every((k) => selected.has(k));
  const selectAll = () => onChange(allKeys);
  const selectNone = () => onChange([]);
  const toggle = (k) => {
    const next = new Set(selected);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    onChange(allKeys.filter((x) => next.has(x)));
  };
  return (
    <div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 6 }}>
        <button type="button" className="btn btn-sm" onClick={selectAll} disabled={allSelected}>
          All
        </button>
        <button type="button" className="btn btn-sm" onClick={selectNone} disabled={selected.size === 0}>
          None
        </button>
        <span style={{ fontSize: 12, color: 'var(--text-secondary)', alignSelf: 'center' }}>
          {selected.size} / {allKeys.length} selected
        </span>
      </div>
      <div
        style={{
          border: '1px solid var(--border)',
          borderRadius: 4,
          padding: 8,
          maxHeight: 160,
          overflowY: 'auto',
        }}
      >
        {options.length === 0 ? (
          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>No options available.</span>
        ) : (
          options.map((opt) => {
            const k = keyOf(opt);
            return (
              <label
                key={k}
                style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', padding: '2px 0' }}
              >
                <input
                  type="checkbox"
                  checked={selected.has(k)}
                  onChange={() => toggle(k)}
                />
                {renderLabel(opt)}
              </label>
            );
          })
        )}
      </div>
    </div>
  );
}

const EMPTY_FORM = {
  token_name: '',
  warehouse_ids: [],
  event_types: [],
  endpoints: [],
  // v1.7.0 Pipe B inbound scope dimensions. source_system is a single
  // select (one allowlist row per token); inbound_resources mirrors
  // the event_types / endpoints checkbox shape; mapping_override is
  // a single capability flag.
  source_system: '',
  inbound_resources: [],
  mapping_override: false,
  advancedMode: false,
  advancedWarehouseIds: '',
  advancedEventTypes: '',
  advancedEndpoints: '',
};

export default function Tokens() {
  const [tokens, setTokens] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [createError, setCreateError] = useState('');
  const [reveal, setReveal] = useState(null);
  const [revealAcked, setRevealAcked] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [pageError, setPageError] = useState('');
  // #159: scope-picker data. Fetched on modal open so fresh
  // deployments without a cached catalog still get correct
  // checkbox options. Empty defaults render "No options" placeholders
  // rather than blowing up before the fetch returns.
  const [scopeCatalog, setScopeCatalog] = useState({
    event_types: [],
    endpoints: [],
    inbound_resources: [],
    source_systems: [],
  });
  const [warehouses, setWarehouses] = useState([]);

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    const res = await api.get('/admin/tokens');
    if (res?.ok) {
      const data = await res.json();
      setTokens(data.tokens || []);
      setPageError('');
    } else {
      setPageError('Failed to load tokens');
    }
    setLoading(false);
  }

  async function openCreate() {
    setForm(EMPTY_FORM);
    setCreateError('');
    setShowCreate(true);
    // Fire both fetches in parallel; either failing falls back to an
    // empty list in the respective checkbox component.
    const [catalogRes, warehousesRes] = await Promise.all([
      api.get('/admin/scope-catalog'),
      api.get('/admin/warehouses'),
    ]);
    if (catalogRes?.ok) {
      const data = await catalogRes.json();
      setScopeCatalog({
        event_types: data.event_types || [],
        endpoints: data.endpoints || [],
        inbound_resources: data.inbound_resources || [],
        source_systems: data.source_systems || [],
      });
    }
    if (warehousesRes?.ok) {
      const data = await warehousesRes.json();
      setWarehouses(data.warehouses || []);
    }
  }

  function parseCsv(raw) {
    return raw.split(',').map(s => s.trim()).filter(Boolean);
  }

  function toggleAdvanced() {
    // When toggling on: seed the advanced text inputs from the
    // current checkbox selections so the admin does not lose work.
    // When toggling off: parse the text back into the checkbox
    // selections for the same reason. Either way, the two
    // representations stay in sync at toggle-time even if the
    // admin edits them in the other mode afterwards.
    setForm((f) => {
      if (!f.advancedMode) {
        return {
          ...f,
          advancedMode: true,
          advancedWarehouseIds: f.warehouse_ids.join(', '),
          advancedEventTypes: f.event_types.join(', '),
          advancedEndpoints: f.endpoints.join(', '),
        };
      }
      const wh_ids = parseCsv(f.advancedWarehouseIds).map(Number).filter(Number.isFinite);
      return {
        ...f,
        advancedMode: false,
        warehouse_ids: wh_ids,
        event_types: parseCsv(f.advancedEventTypes),
        endpoints: parseCsv(f.advancedEndpoints),
      };
    });
  }

  async function submitCreate() {
    setCreateError('');
    if (!form.token_name.trim()) { setCreateError('Name is required'); return; }

    // #159: in advanced mode, parse text inputs at submit time so
    // the admin can tweak right up to the Create click. In
    // checkbox mode, the arrays are already maintained in form state.
    let wh_ids;
    let event_types;
    let endpoints;
    if (form.advancedMode) {
      wh_ids = parseCsv(form.advancedWarehouseIds).map(s => Number(s)).filter(n => Number.isInteger(n) && n > 0);
      if (form.advancedWarehouseIds.trim() && wh_ids.length === 0) {
        setCreateError('Warehouse IDs must be comma-separated positive integers');
        return;
      }
      event_types = parseCsv(form.advancedEventTypes);
      endpoints = parseCsv(form.advancedEndpoints);
    } else {
      wh_ids = form.warehouse_ids;
      event_types = form.event_types;
      endpoints = form.endpoints;
    }

    // v1.7.0 Pipe B: at least one direction must be set. Either
    // endpoints (outbound, v1.5 shape) OR source_system + inbound_resources
    // (inbound). Both is valid (connector-framework shape at v1.9).
    const hasOutbound = endpoints.length > 0;
    const hasInbound = !!form.source_system && form.inbound_resources.length > 0;
    if (!hasOutbound && !hasInbound) {
      setCreateError(
        'At least one direction is required: either Endpoints (outbound) ' +
        'or Source system + Inbound resources (inbound).'
      );
      return;
    }
    if (!!form.source_system !== form.inbound_resources.length > 0) {
      // XOR: half-configured inbound is rejected by the server too.
      setCreateError(
        'Source system and Inbound resources must be set together; ' +
        'leave both empty for an outbound-only token.'
      );
      return;
    }
    if (form.mapping_override && form.inbound_resources.length === 0) {
      setCreateError(
        'mapping_override capability only applies to inbound tokens.'
      );
      return;
    }
    const payload = {
      token_name: form.token_name.trim(),
      warehouse_ids: wh_ids,
      event_types,
      endpoints,
      source_system: form.source_system || null,
      inbound_resources: form.inbound_resources,
      mapping_override: form.mapping_override,
    };
    const res = await api.post('/admin/tokens', payload);
    const body = await res?.json();
    if (res?.ok) {
      setShowCreate(false);
      setReveal({ kind: 'issued', token: body.token, token_name: body.token_name });
      setRevealAcked(false);
      load();
    } else {
      setCreateError(body?.error || 'Failed to create token');
    }
  }

  async function rotate(row) {
    const res = await api.post(`/admin/tokens/${row.token_id}/rotate`, {});
    const body = await res?.json();
    if (res?.ok) {
      setReveal({ kind: 'rotated', token: body.token, token_name: row.token_name });
      setRevealAcked(false);
      load();
    } else {
      setPageError(body?.error || 'Rotation failed');
    }
  }

  async function revoke(row) {
    const res = await api.post(`/admin/tokens/${row.token_id}/revoke`, {});
    if (res?.ok) {
      setConfirmRevoke(null);
      load();
    } else {
      const body = await res?.json();
      setPageError(body?.error || 'Revoke failed');
      setConfirmRevoke(null);
    }
  }

  async function del(row) {
    const res = await api.delete(`/admin/tokens/${row.token_id}`);
    if (res?.ok) {
      setConfirmDelete(null);
      load();
    } else {
      const body = await res?.json();
      setPageError(body?.error || 'Delete failed');
      setConfirmDelete(null);
    }
  }

  async function copyToken() {
    if (!reveal?.token) return;
    // Clipboard API may be unavailable (older iOS, http:// dev origins).
    // The raw value is still visible on-screen; the copy button just
    // becomes inert rather than raising.
    try {
      await navigator.clipboard.writeText(reveal.token);
    } catch {
      /* noop */
    }
  }

  const columns = [
    { key: 'token_name', label: 'Name' },
    {
      key: 'status',
      label: 'Status',
      render: (r) => {
        const b = STATUS_BADGE[r.status];
        return b ? <Badge label={b.label} color={b.color} /> : r.status;
      },
    },
    {
      key: 'rotation_status',
      label: 'Rotation',
      render: (r) => {
        const b = ROTATION_BADGE[r.rotation_status];
        if (!b) return <span style={{ color: 'var(--text-secondary)' }}>—</span>;
        return <Badge label={b.label} color={b.color} />;
      },
    },
    { key: 'warehouse_ids', label: 'Warehouses', render: (r) => renderCsv(r.warehouse_ids) },
    { key: 'event_types', label: 'Event types', render: (r) => renderCsv(r.event_types) },
    { key: 'endpoints', label: 'Endpoints', render: (r) => renderCsv(r.endpoints) },
    {
      key: 'source_system',
      label: 'Source',
      render: (r) =>
        r.source_system
          ? <span className="mono" style={{ fontSize: 12 }}>{r.source_system}</span>
          : <span style={{ color: 'var(--text-secondary)' }}>—</span>,
    },
    { key: 'inbound_resources', label: 'Inbound', render: (r) => renderCsv(r.inbound_resources) },
    {
      key: 'expires_at',
      label: 'Expires',
      render: (r) => r.expires_at ? new Date(r.expires_at).toLocaleDateString() : '—',
    },
    {
      key: 'actions',
      label: '',
      render: (r) => (
        <div style={{ display: 'flex', gap: 4 }}>
          <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); rotate(r); }}
                  disabled={r.status !== 'active'} title="Rotate">↻</button>
          <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); setConfirmRevoke(r); }}
                  disabled={r.status !== 'active'} title="Revoke">⊘</button>
          <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); setConfirmDelete(r); }}
                  aria-label="Delete" title="Delete">&#128465;</button>
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title="API tokens">
        <button className="btn btn-primary" onClick={openCreate}>New token</button>
      </PageHeader>

      {pageError && <div className="form-error" style={{ marginBottom: 12 }}>{pageError}</div>}

      <DataTable
        columns={columns}
        data={tokens}
        emptyMessage={loading ? 'Loading…' : 'No API tokens issued yet'}
      />

      {showCreate && (
        <Modal
          title="New API token"
          onClose={() => setShowCreate(false)}
          footer={
            <>
              <button className="btn" onClick={() => setShowCreate(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={submitCreate}>Create</button>
            </>
          }
        >
          {createError && <div className="form-error" style={{ marginBottom: 12 }}>{createError}</div>}
          <div className="form-group">
            <label>Name</label>
            <input
              className="form-input"
              value={form.token_name}
              onChange={(e) => setForm({ ...form, token_name: e.target.value })}
              placeholder="fabric-prod"
            />
          </div>

          {/* #159: default (checkbox-driven) path. Hidden when the
              admin opens the Advanced disclosure below. */}
          {!form.advancedMode && (
            <>
              <div className="form-group">
                <label>Warehouses</label>
                <ScopeCheckboxList
                  options={warehouses}
                  value={form.warehouse_ids}
                  onChange={(ids) => setForm((f) => ({ ...f, warehouse_ids: ids }))}
                  keyOf={(w) => w.warehouse_id}
                  renderLabel={(w) => (
                    <span>
                      <span className="mono">{w.warehouse_code}</span>
                      {w.warehouse_name ? ` - ${w.warehouse_name}` : ''}
                    </span>
                  )}
                />
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                  Empty selection denies access to every warehouse.
                </div>
              </div>
              <div className="form-group">
                <label>Event types</label>
                <ScopeCheckboxList
                  options={scopeCatalog.event_types}
                  value={form.event_types}
                  onChange={(types) => setForm((f) => ({ ...f, event_types: types }))}
                  keyOf={(t) => t}
                  renderLabel={(t) => <span className="mono">{t}</span>}
                />
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                  Empty selection denies every event_type.
                </div>
              </div>
              <div className="form-group">
                <label>Endpoints</label>
                <ScopeCheckboxList
                  options={scopeCatalog.endpoints}
                  value={form.endpoints}
                  onChange={(slugs) => setForm((f) => ({ ...f, endpoints: slugs }))}
                  keyOf={(s) => s}
                  renderLabel={(s) => <span className="mono">{s}</span>}
                />
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                  Outbound v1 routes. Leave empty for an inbound-only token.
                </div>
              </div>

              {/* v1.7.0 Pipe B inbound scope. Source-system dropdown is
                  populated from inbound_source_systems_allowlist via
                  /admin/scope-catalog; admins cannot type a value the
                  FK would reject. */}
              <div className="form-group" style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
                <label htmlFor="token-source-system">Source system (inbound)</label>
                <select
                  id="token-source-system"
                  aria-label="Source system"
                  className="form-input"
                  value={form.source_system}
                  onChange={(e) => setForm((f) => ({ ...f, source_system: e.target.value }))}
                >
                  <option value="">— None (outbound-only token) —</option>
                  {scopeCatalog.source_systems.map((s) => (
                    <option key={s.source_system} value={s.source_system}>
                      {s.source_system} ({s.kind})
                    </option>
                  ))}
                </select>
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                  Required for inbound tokens. New entries land via the
                  operator SQL recipe at docs/runbooks/inbound-source-systems.md.
                </div>
              </div>
              <div className="form-group">
                <label>Inbound resources</label>
                <ScopeCheckboxList
                  options={scopeCatalog.inbound_resources}
                  value={form.inbound_resources}
                  onChange={(rs) => setForm((f) => ({ ...f, inbound_resources: rs }))}
                  keyOf={(s) => s}
                  renderLabel={(s) => <span className="mono">{s}</span>}
                />
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                  Inbound resource scope. Required when Source system is set.
                  Empty selection denies every inbound resource.
                </div>
              </div>
              <div className="form-group">
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={form.mapping_override}
                    onChange={(e) => setForm((f) => ({ ...f, mapping_override: e.target.checked }))}
                  />
                  <span>Allow mapping_overrides (capability flag)</span>
                </label>
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                  Reserved for v1.7.1. Granting this capability has no effect in
                  v1.7.0; requests with mapping_overrides return 403 regardless.
                </div>
              </div>
            </>
          )}

          {/* #159: advanced escape hatch. Collapsed by default so
              the common case stays checkbox-driven. Shown when the
              admin needs to paste a scope from docs or scripting. */}
          <div className="form-group" style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={form.advancedMode}
                onChange={toggleAdvanced}
                aria-label="Advanced: paste comma-separated values"
              />
              <span style={{ fontSize: 13 }}>Advanced: paste comma-separated values</span>
            </label>
          </div>
          {form.advancedMode && (
            <>
              <div className="form-group">
                <label>Warehouse IDs</label>
                <input
                  className="form-input"
                  value={form.advancedWarehouseIds}
                  onChange={(e) => setForm({ ...form, advancedWarehouseIds: e.target.value })}
                  placeholder="1, 2"
                />
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                  Comma-separated integer IDs. Empty = no warehouse access.
                </div>
              </div>
              <div className="form-group">
                <label>Event types</label>
                <input
                  className="form-input"
                  value={form.advancedEventTypes}
                  onChange={(e) => setForm({ ...form, advancedEventTypes: e.target.value })}
                  placeholder="receipt.completed, ship.confirmed"
                />
              </div>
              <div className="form-group">
                <label>Endpoints</label>
                <input
                  className="form-input"
                  value={form.advancedEndpoints}
                  onChange={(e) => setForm({ ...form, advancedEndpoints: e.target.value })}
                  placeholder="events.poll, snapshot.inventory"
                />
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                  Required. Comma-separated slugs; the token can hit only the v1 routes listed.
                </div>
              </div>
            </>
          )}
        </Modal>
      )}

      {reveal && (
        <Modal
          title={reveal.kind === 'issued' ? 'Token issued' : 'Token rotated'}
          onClose={() => { /* reveal modal must be explicitly acknowledged */ }}
          footer={
            <button
              className="btn btn-primary"
              onClick={() => setReveal(null)}
              disabled={!revealAcked}
              title={revealAcked ? 'Close' : 'Confirm you have saved the token first'}
            >
              Close
            </button>
          }
        >
          <p style={{ fontSize: 13, fontWeight: 600 }}>
            {reveal.token_name}: this value is shown exactly once. Copy it to
            your connector's configuration now. Sentry stores only the hash;
            if you lose this value you must rotate.
          </p>
          <div style={{
            background: 'var(--surface-muted)',
            border: '1px solid var(--border)',
            borderRadius: 4,
            padding: 12,
            marginTop: 12,
            wordBreak: 'break-all',
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 12,
          }}>
            {reveal.token}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <button className="btn" onClick={copyToken}>Copy to clipboard</button>
          </div>
          <div className="form-group" style={{ marginTop: 16 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={revealAcked}
                onChange={(e) => setRevealAcked(e.target.checked)}
              />
              I have saved this token in a secure location.
            </label>
          </div>
        </Modal>
      )}

      {confirmRevoke && (
        <Modal
          title="Revoke token"
          onClose={() => setConfirmRevoke(null)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmRevoke(null)}>Cancel</button>
              <button className="btn btn-primary" style={{ background: 'var(--copper)' }}
                      onClick={() => revoke(confirmRevoke)}>Revoke</button>
            </>
          }
        >
          <p style={{ fontSize: 13, fontWeight: 600 }}>
            Revoke {confirmRevoke.token_name}? The token stops authenticating
            within seconds across every API worker (Redis pubsub eviction);
            the 60-second cache TTL is the backstop if the pubsub channel
            is unavailable. The row remains in the list with status=revoked;
            delete it separately when you want it removed.
          </p>
        </Modal>
      )}

      {confirmDelete && (
        <Modal
          title="Delete token"
          onClose={() => setConfirmDelete(null)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmDelete(null)}>Cancel</button>
              <button className="btn btn-primary" style={{ background: 'var(--copper)' }}
                      onClick={() => del(confirmDelete)}>Delete</button>
            </>
          }
        >
          <p style={{ fontSize: 13, fontWeight: 600, color: 'var(--danger)' }}>
            Permanently delete {confirmDelete.token_name}? This removes the
            row entirely. Prefer Revoke if you only need to stop access;
            revoked rows preserve the audit trail.
          </p>
        </Modal>
      )}
    </div>
  );
}
