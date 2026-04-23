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

export default function Tokens() {
  const [tokens, setTokens] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({
    token_name: '',
    warehouse_ids: '',
    event_types: '',
    endpoints: '',
  });
  const [createError, setCreateError] = useState('');
  const [reveal, setReveal] = useState(null);
  const [revealAcked, setRevealAcked] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [pageError, setPageError] = useState('');

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

  function openCreate() {
    setForm({ token_name: '', warehouse_ids: '', event_types: '', endpoints: '' });
    setCreateError('');
    setShowCreate(true);
  }

  function parseCsv(raw) {
    return raw.split(',').map(s => s.trim()).filter(Boolean);
  }

  async function submitCreate() {
    setCreateError('');
    if (!form.token_name.trim()) { setCreateError('Name is required'); return; }
    const wh_ids = parseCsv(form.warehouse_ids).map(s => Number(s)).filter(n => Number.isInteger(n) && n > 0);
    if (form.warehouse_ids.trim() && wh_ids.length === 0) {
      setCreateError('Warehouse IDs must be comma-separated positive integers');
      return;
    }
    const endpoints = parseCsv(form.endpoints);
    // v1.5.1 V-200 (#140): endpoints is a required non-empty list of
    // known slugs. The server validates but we short-circuit here so
    // the admin gets immediate feedback instead of a generic 400.
    if (endpoints.length === 0) {
      setCreateError('Endpoints is required. Use "Grant all v1 endpoints" or list the slugs explicitly.');
      return;
    }
    const payload = {
      token_name: form.token_name.trim(),
      warehouse_ids: wh_ids,
      event_types: parseCsv(form.event_types),
      endpoints,
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

  // v1.5.1 V-200 (#140): one-click preset so operators who want a
  // token with no endpoint restriction don't have to hand-type five
  // slugs. Matches the set the server validates against; update
  // both sides when a new /api/v1/* route ships.
  const ALL_V1_ENDPOINT_SLUGS = [
    'events.poll',
    'events.ack',
    'events.types',
    'events.schema',
    'snapshot.inventory',
  ];
  function grantAllEndpoints() {
    setForm((f) => ({ ...f, endpoints: ALL_V1_ENDPOINT_SLUGS.join(', ') }));
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
          <div className="form-group">
            <label>Warehouse IDs</label>
            <input
              className="form-input"
              value={form.warehouse_ids}
              onChange={(e) => setForm({ ...form, warehouse_ids: e.target.value })}
              placeholder="1, 2"
            />
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
              Comma-separated integer IDs. Empty = token has no warehouse access.
            </div>
          </div>
          <div className="form-group">
            <label>Event types</label>
            <input
              className="form-input"
              value={form.event_types}
              onChange={(e) => setForm({ ...form, event_types: e.target.value })}
              placeholder="receipt.completed, ship.confirmed"
            />
          </div>
          <div className="form-group">
            <label>Endpoints</label>
            <input
              className="form-input"
              value={form.endpoints}
              onChange={(e) => setForm({ ...form, endpoints: e.target.value })}
              placeholder="events.poll, snapshot.inventory"
            />
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
              Required. Comma-separated slugs; the token can hit only the v1 routes listed.
              Valid: <span className="mono">events.poll</span>,{' '}
              <span className="mono">events.ack</span>,{' '}
              <span className="mono">events.types</span>,{' '}
              <span className="mono">events.schema</span>,{' '}
              <span className="mono">snapshot.inventory</span>.
              <button
                type="button"
                className="btn btn-sm"
                onClick={grantAllEndpoints}
                style={{ marginLeft: 8 }}
              >
                Grant all v1 endpoints
              </button>
            </div>
          </div>
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
