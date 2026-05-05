import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

// v1.7.0 plan §4.2: read-only Inbound observability page. Lists the
// last N rows across all five inbound_<resource> staging tables with
// filters for source_system / resource / status. Detail view shows
// source_payload + canonical_payload + ingest metadata. v1.7 ships
// no replay / edit / manual-fix UI -- per plan §4.3 the canonical
// fix path is operator SQL with audit_log; admin UI for inbound data
// fixes lands once NetSuite (v2.0) reveals what fix workflows are
// actually needed.

const RESOURCE_OPTIONS = [
  { value: '', label: 'All resources' },
  { value: 'sales_orders', label: 'sales_orders' },
  { value: 'items', label: 'items' },
  { value: 'customers', label: 'customers' },
  { value: 'vendors', label: 'vendors' },
  { value: 'purchase_orders', label: 'purchase_orders' },
];

const STATUS_OPTIONS = [
  { value: '', label: 'All statuses' },
  { value: 'applied', label: 'applied' },
  { value: 'superseded', label: 'superseded' },
];

function fmtTimestamp(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function InboundActivity() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [filters, setFilters] = useState({
    source_system: '',
    resource: '',
    status: '',
  });
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    setError('');
    const params = new URLSearchParams();
    if (filters.source_system) params.set('source_system', filters.source_system);
    if (filters.resource) params.set('resource', filters.resource);
    if (filters.status) params.set('status', filters.status);
    const qs = params.toString() ? `?${params.toString()}` : '';
    const res = await api.get(`/admin/inbound/activity${qs}`);
    if (res?.ok) {
      const data = await res.json();
      setRows(data.rows || []);
    } else {
      const body = await res?.json();
      setError(body?.error || 'Failed to load inbound activity');
    }
    setLoading(false);
  }

  async function openDetail(row) {
    setDetailLoading(true);
    setDetail({ summary: row });
    const res = await api.get(
      `/admin/inbound/activity/${row.resource}/${row.inbound_id}`,
    );
    if (res?.ok) {
      const data = await res.json();
      setDetail({ summary: row, full: data });
    } else {
      const body = await res?.json();
      setDetail({ summary: row, error: body?.error || 'Failed to load detail' });
    }
    setDetailLoading(false);
  }

  const columns = [
    {
      key: 'received_at',
      label: 'Received',
      render: (r) => (
        <span className="mono" style={{ fontSize: 12 }}>
          {fmtTimestamp(r.received_at)}
        </span>
      ),
    },
    {
      key: 'resource',
      label: 'Resource',
      render: (r) => <span className="mono" style={{ fontSize: 12 }}>{r.resource}</span>,
    },
    {
      key: 'source_system',
      label: 'Source system',
      render: (r) => <span className="mono" style={{ fontSize: 12 }}>{r.source_system}</span>,
    },
    {
      key: 'external_id',
      label: 'External ID',
      render: (r) => <span className="mono" style={{ fontSize: 12 }}>{r.external_id}</span>,
    },
    {
      key: 'external_version',
      label: 'Version',
      render: (r) => (
        <span className="mono" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
          {r.external_version}
        </span>
      ),
    },
    {
      key: 'status',
      label: 'Status',
      render: (r) => (
        <span style={{
          fontSize: 11,
          fontWeight: 600,
          color: r.status === 'applied' ? 'var(--text-secondary)' : 'var(--danger)',
        }}>
          {r.status}
        </span>
      ),
    },
    {
      key: 'actions',
      label: '',
      render: (r) => (
        <button
          className="btn btn-sm"
          onClick={(e) => { e.stopPropagation(); openDetail(r); }}
        >
          View
        </button>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title="Inbound activity">
        <button className="btn" onClick={load} disabled={loading}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </PageHeader>

      {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}

      {/* Filter row -- inline so the common case (one source_system,
          one resource) stays a single click + select. */}
      <div style={{
        display: 'flex',
        gap: 12,
        alignItems: 'flex-end',
        marginBottom: 16,
        padding: 12,
        background: 'var(--surface-muted)',
        borderRadius: 4,
      }}>
        <div style={{ flex: 1 }}>
          <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
            Source system
          </label>
          <input
            className="form-input"
            value={filters.source_system}
            onChange={(e) => setFilters({ ...filters, source_system: e.target.value })}
            placeholder="exact match (e.g. fabric)"
          />
        </div>
        <div>
          <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
            Resource
          </label>
          <select
            className="form-input"
            value={filters.resource}
            onChange={(e) => setFilters({ ...filters, resource: e.target.value })}
          >
            {RESOURCE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
        <div>
          <label style={{ display: 'block', fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
            Status
          </label>
          <select
            className="form-input"
            value={filters.status}
            onChange={(e) => setFilters({ ...filters, status: e.target.value })}
          >
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
        <button className="btn btn-primary" onClick={load}>Apply</button>
      </div>

      <DataTable
        columns={columns}
        data={rows}
        emptyMessage={loading ? 'Loading…' : 'No inbound activity matches the current filters.'}
      />

      {detail && (
        <Modal
          title={`Inbound row: ${detail.summary.resource} #${detail.summary.inbound_id}`}
          onClose={() => setDetail(null)}
          footer={<button className="btn" onClick={() => setDetail(null)}>Close</button>}
        >
          {detailLoading && <div>Loading…</div>}
          {detail.error && <div className="form-error">{detail.error}</div>}
          {detail.full && (
            <div>
              <div style={{ display: 'grid', gridTemplateColumns: '160px 1fr', gap: 6, marginBottom: 12, fontSize: 13 }}>
                <div style={{ color: 'var(--text-secondary)' }}>Received at</div>
                <div className="mono">{fmtTimestamp(detail.full.received_at)}</div>
                <div style={{ color: 'var(--text-secondary)' }}>Status</div>
                <div className="mono">{detail.full.status}</div>
                <div style={{ color: 'var(--text-secondary)' }}>Superseded at</div>
                <div className="mono">{fmtTimestamp(detail.full.superseded_at)}</div>
                <div style={{ color: 'var(--text-secondary)' }}>Source system</div>
                <div className="mono">{detail.full.source_system}</div>
                <div style={{ color: 'var(--text-secondary)' }}>External ID</div>
                <div className="mono">{detail.full.external_id}</div>
                <div style={{ color: 'var(--text-secondary)' }}>External version</div>
                <div className="mono">{detail.full.external_version}</div>
                <div style={{ color: 'var(--text-secondary)' }}>Canonical ID</div>
                <div className="mono" style={{ wordBreak: 'break-all' }}>{detail.full.canonical_id}</div>
                <div style={{ color: 'var(--text-secondary)' }}>Token ID</div>
                <div className="mono">{detail.full.ingested_via_token_id}</div>
              </div>
              <PayloadBlock title="Source payload" value={detail.full.source_payload} />
              <PayloadBlock title="Canonical payload" value={detail.full.canonical_payload} />
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}

function PayloadBlock({ title, value }) {
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{title}</div>
      <pre style={{
        background: 'var(--surface-muted)',
        border: '1px solid var(--border)',
        borderRadius: 4,
        padding: 10,
        fontSize: 12,
        maxHeight: 320,
        overflow: 'auto',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}
