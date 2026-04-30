import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

const STATUS_BADGE = {
  active: { label: 'active', color: 'var(--text-secondary)' },
  paused: { label: 'paused', color: '#c49100' },
  revoked: { label: 'revoked', color: 'var(--danger)' },
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

// Mirrors the Tokens.jsx (#159) checkbox picker: the admin clicks
// boxes against the authoritative scope-catalog instead of typing
// slugs from memory. Inlined here rather than extracted because the
// renderLabel / keyOf shape is just different enough that a shared
// component would need three configuration props for each call site.
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

function Slider({ label, min, max, step, value, onChange, hint }) {
  return (
    <div className="form-group">
      <label style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span>{label}</span>
        <span className="mono" style={{ fontSize: 12 }}>{value}</span>
      </label>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: '100%' }}
      />
      {hint && (
        <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
          {hint}
        </div>
      )}
    </div>
  );
}

const EMPTY_FORM = {
  display_name: '',
  connector_id: '',
  delivery_url: '',
  event_types: [],
  warehouse_ids: [],
  rate_limit_per_second: 50,
  pending_ceiling: 10000,
  dlq_ceiling: 1000,
  acknowledge_url_reuse: false,
};

function formatRate(rate) {
  if (rate === null || rate === undefined) return '—';
  return `${(rate * 100).toFixed(1)}%`;
}

export default function Webhooks() {
  const [webhooks, setWebhooks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [createError, setCreateError] = useState('');
  const [reveal, setReveal] = useState(null);
  const [revealAcked, setRevealAcked] = useState(false);
  const [urlReuseGate, setUrlReuseGate] = useState(null);
  const [connectors, setConnectors] = useState([]);
  const [scopeCatalog, setScopeCatalog] = useState({ event_types: [] });
  const [warehouses, setWarehouses] = useState([]);

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    const res = await api.get('/admin/webhooks');
    if (res?.ok) {
      const data = await res.json();
      setWebhooks(data.webhooks || []);
      setPageError('');
    } else {
      setPageError('Failed to load webhooks');
    }
    setLoading(false);
  }

  async function openCreate() {
    setForm(EMPTY_FORM);
    setCreateError('');
    setUrlReuseGate(null);
    setShowCreate(true);
    // Fire all three picker fetches in parallel; a missing list
    // renders the inner ScopeCheckboxList "No options available"
    // placeholder rather than blocking the modal.
    const [connRes, catalogRes, whRes] = await Promise.all([
      api.get('/admin/connector-registry'),
      api.get('/admin/scope-catalog'),
      api.get('/admin/warehouses'),
    ]);
    if (connRes?.ok) {
      const data = await connRes.json();
      setConnectors(data.connectors || []);
    }
    if (catalogRes?.ok) {
      const data = await catalogRes.json();
      setScopeCatalog({ event_types: data.event_types || [] });
    }
    if (whRes?.ok) {
      const data = await whRes.json();
      setWarehouses(data.warehouses || []);
    }
  }

  function buildPayload() {
    // Only include filter keys the admin actually picked. The
    // dispatcher's strict-typed Pydantic model treats absent keys
    // as "no filter on this dimension"; sending an empty array
    // would mean "match nothing on this dimension" which is the
    // wrong default for an admin who simply did not check any boxes.
    const subscription_filter = {};
    if (form.event_types.length > 0) subscription_filter.event_types = form.event_types;
    if (form.warehouse_ids.length > 0) subscription_filter.warehouse_ids = form.warehouse_ids;
    return {
      connector_id: form.connector_id,
      display_name: form.display_name.trim(),
      delivery_url: form.delivery_url.trim(),
      subscription_filter,
      rate_limit_per_second: form.rate_limit_per_second,
      pending_ceiling: form.pending_ceiling,
      dlq_ceiling: form.dlq_ceiling,
      acknowledge_url_reuse: form.acknowledge_url_reuse,
    };
  }

  async function submitCreate() {
    setCreateError('');
    if (!form.display_name.trim()) { setCreateError('Display name is required'); return; }
    if (!form.connector_id) { setCreateError('Connector is required'); return; }
    if (!form.delivery_url.trim()) { setCreateError('Delivery URL is required'); return; }
    const res = await api.post('/admin/webhooks', buildPayload());
    const body = await res?.json();
    if (res?.status === 409 && body?.error === 'url_reuse_tombstone') {
      // Server-side URL-reuse gate. Stash the tombstone_id so the
      // dedicated modal can surface the deletion history; on
      // confirm the create re-submits with acknowledge_url_reuse.
      setUrlReuseGate({ tombstone_id: body.tombstone_id, detail: body.detail });
      return;
    }
    if (res?.ok) {
      setShowCreate(false);
      setReveal({
        display_name: body.display_name,
        secret: body.secret,
        secret_generation: body.secret_generation,
      });
      setRevealAcked(false);
      load();
      return;
    }
    setCreateError(body?.error || 'Failed to create webhook');
  }

  async function confirmUrlReuse() {
    setForm((f) => ({ ...f, acknowledge_url_reuse: true }));
    setUrlReuseGate(null);
    // Re-submit immediately with the acknowledgement flag flipped
    // on. Building the payload directly here avoids a stale-closure
    // hazard if React batched the state update.
    const payload = { ...buildPayload(), acknowledge_url_reuse: true };
    const res = await api.post('/admin/webhooks', payload);
    const body = await res?.json();
    if (res?.ok) {
      setShowCreate(false);
      setReveal({
        display_name: body.display_name,
        secret: body.secret,
        secret_generation: body.secret_generation,
      });
      setRevealAcked(false);
      load();
    } else {
      setCreateError(body?.error || 'Failed to create webhook');
    }
  }

  async function copySecret() {
    if (!reveal?.secret) return;
    try {
      await navigator.clipboard.writeText(reveal.secret);
    } catch {
      /* clipboard API unavailable; the secret is still visible on-screen */
    }
  }

  const httpsValid = form.delivery_url.trim().toLowerCase().startsWith('https://');
  const httpAttempt = form.delivery_url.trim().toLowerCase().startsWith('http://');

  const columns = [
    { key: 'display_name', label: 'Name' },
    { key: 'connector_id', label: 'Connector', render: (r) => <span className="mono">{r.connector_id}</span> },
    {
      key: 'status',
      label: 'Status',
      render: (r) => {
        const b = STATUS_BADGE[r.status];
        return b ? <Badge label={b.label} color={b.color} /> : r.status;
      },
    },
    {
      key: 'pause_reason',
      label: 'Pause reason',
      render: (r) => r.pause_reason
        ? <span className="mono" style={{ fontSize: 12 }}>{r.pause_reason}</span>
        : <span style={{ color: 'var(--text-secondary)' }}>—</span>,
    },
    {
      key: 'success_rate_24h',
      label: 'Success (24h)',
      render: (r) => formatRate(r.stats?.success_rate_24h),
    },
    {
      key: 'pending_count',
      label: 'Pending',
      render: (r) => <span className="mono">{r.stats?.pending_count ?? 0}</span>,
    },
    {
      key: 'delivery_url',
      label: 'URL',
      render: (r) => (
        <span className="mono" style={{ fontSize: 12, wordBreak: 'break-all' }}>
          {r.delivery_url}
        </span>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title="Webhooks">
        <button className="btn btn-primary" onClick={openCreate}>New webhook</button>
      </PageHeader>

      {pageError && <div className="form-error" style={{ marginBottom: 12 }}>{pageError}</div>}

      <DataTable
        columns={columns}
        data={webhooks}
        emptyMessage={loading ? 'Loading…' : 'No webhook subscriptions yet'}
      />

      {showCreate && (
        <Modal
          title="New webhook subscription"
          onClose={() => setShowCreate(false)}
          footer={
            <>
              <button className="btn" onClick={() => setShowCreate(false)}>Cancel</button>
              <button
                className="btn btn-primary"
                onClick={submitCreate}
                disabled={!httpsValid && !httpAttempt}
              >
                Create
              </button>
            </>
          }
        >
          {createError && <div className="form-error" style={{ marginBottom: 12 }}>{createError}</div>}

          <div className="form-group">
            <label>Display name</label>
            <input
              className="form-input"
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
              placeholder="fabric-prod"
            />
          </div>

          <div className="form-group">
            <label>Connector</label>
            <select
              className="form-input"
              value={form.connector_id}
              onChange={(e) => setForm({ ...form, connector_id: e.target.value })}
            >
              <option value="">Select a connector…</option>
              {connectors.map((c) => (
                <option key={c.connector_id} value={c.connector_id}>
                  {c.connector_id} - {c.display_name}
                </option>
              ))}
            </select>
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
              Register a new connector under Consumer groups before it appears here.
            </div>
          </div>

          <div className="form-group">
            <label>Delivery URL</label>
            <input
              className="form-input"
              value={form.delivery_url}
              onChange={(e) => setForm({ ...form, delivery_url: e.target.value })}
              placeholder="https://example.com/webhooks/sentry"
            />
            {httpAttempt && (
              <div className="form-error" style={{ marginTop: 4, fontSize: 12 }}>
                http:// is rejected in production. Use https:// or set
                SENTRY_ALLOW_HTTP_WEBHOOKS=true in dev / CI.
              </div>
            )}
          </div>

          <div className="form-group">
            <label>Event types (filter)</label>
            <ScopeCheckboxList
              options={scopeCatalog.event_types}
              value={form.event_types}
              onChange={(types) => setForm((f) => ({ ...f, event_types: types }))}
              keyOf={(t) => t}
              renderLabel={(t) => <span className="mono">{t}</span>}
            />
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
              Empty selection = match every event type.
            </div>
          </div>

          <div className="form-group">
            <label>Warehouses (filter)</label>
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
              Empty selection = match every warehouse.
            </div>
          </div>

          <Slider
            label="Rate limit (req/sec)"
            min={1}
            max={100}
            step={1}
            value={form.rate_limit_per_second}
            onChange={(v) => setForm({ ...form, rate_limit_per_second: v })}
            hint="Token-bucket cap on POST throughput per subscription."
          />

          <Slider
            label="Pending ceiling"
            min={100}
            max={100000}
            step={100}
            value={form.pending_ceiling}
            onChange={(v) => setForm({ ...form, pending_ceiling: v })}
            hint="Auto-pauses the subscription when pending+in_flight reaches this. Bounded by DISPATCHER_MAX_PENDING_HARD_CAP."
          />

          <Slider
            label="DLQ ceiling"
            min={10}
            max={10000}
            step={10}
            value={form.dlq_ceiling}
            onChange={(v) => setForm({ ...form, dlq_ceiling: v })}
            hint="Auto-pauses the subscription when DLQ depth reaches this. Bounded by DISPATCHER_MAX_DLQ_HARD_CAP."
          />
        </Modal>
      )}

      {urlReuseGate && (
        <Modal
          title="URL previously used"
          onClose={() => setUrlReuseGate(null)}
          footer={
            <>
              <button className="btn" onClick={() => setUrlReuseGate(null)}>Cancel</button>
              <button
                className="btn btn-primary"
                style={{ background: 'var(--copper)' }}
                onClick={confirmUrlReuse}
              >
                Acknowledge and create
              </button>
            </>
          }
        >
          <p style={{ fontSize: 13, fontWeight: 600 }}>
            This delivery URL was associated with a previously-deleted
            subscription (tombstone {urlReuseGate.tombstone_id}). The
            previous consumer may still be configured to receive
            traffic at this URL.
          </p>
          <p style={{ fontSize: 13 }}>
            {urlReuseGate.detail}
          </p>
        </Modal>
      )}

      {reveal && (
        <Modal
          title="Webhook secret issued"
          onClose={() => { /* reveal modal must be explicitly acknowledged */ }}
          footer={
            <button
              className="btn btn-primary"
              onClick={() => setReveal(null)}
              disabled={!revealAcked}
              title={revealAcked ? 'Close' : 'Confirm you have saved the secret first'}
            >
              Close
            </button>
          }
        >
          <p style={{ fontSize: 13, fontWeight: 600 }}>
            {reveal.display_name}: this secret (generation {reveal.secret_generation})
            is shown exactly once. Copy it to the consumer's HMAC verifier
            now. Sentry stores only the encrypted form; if you lose this
            value you must rotate.
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
            {reveal.secret}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
            <button className="btn" onClick={copySecret}>Copy to clipboard</button>
          </div>
          <div className="form-group" style={{ marginTop: 16 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={revealAcked}
                onChange={(e) => setRevealAcked(e.target.checked)}
              />
              I have saved this secret in a secure location.
            </label>
          </div>
        </Modal>
      )}
    </div>
  );
}
