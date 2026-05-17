import { useState, useEffect, useMemo } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

// v1.8.0 (#299) productivity dashboard. Reads /api/v1/dashboard/
// productivity for the warehouse-scoped per-user metrics, and
// /api/v1/dashboard/preferences for the per-user chart_order +
// default_range + default_view. Single-file pattern matches
// SalesOrders.jsx + TransferOrders.jsx; the directory layout in
// plan section 5.4 was deferred for codebase uniformity.

const COLOR_TOP = '#8e2715';   // Sentry red (top performer per card)
const COLOR_OTHER = '#c4722a'; // Copper (every other user)

const EVENT_LABELS = {
  // P11.1: picking is now measured in distinct orders, not units.
  picking:       { title: 'Picking',      unit: 'orders' },
  packing:       { title: 'Packing',      unit: 'units' },
  shipped:       { title: 'Shipped',      unit: 'orders' },
  received_skus: { title: 'Received',     unit: 'unique SKUs' },
  putaway_skus:  { title: 'Put Away',     unit: 'unique SKUs' },
};

const RANGE_PRESETS = [
  { key: 'today',     label: 'Today' },
  { key: 'yesterday', label: 'Yesterday' },
  { key: 'last_7d',   label: 'Last 7d' },
  { key: 'last_30d',  label: 'Last 30d' },
  { key: 'custom',    label: 'Custom' },
];

function isoDate(d) {
  // Local YYYY-MM-DD (avoids the toISOString() UTC shift that pushes
  // late-evening dates back a day in non-UTC zones).
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${dd}`;
}

function rangeForPreset(key, customStart, customEnd) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (key === 'today') return { start: isoDate(today), end: isoDate(today) };
  if (key === 'yesterday') {
    const y = new Date(today); y.setDate(y.getDate() - 1);
    return { start: isoDate(y), end: isoDate(y) };
  }
  if (key === 'last_7d') {
    const s = new Date(today); s.setDate(s.getDate() - 6);
    return { start: isoDate(s), end: isoDate(today) };
  }
  if (key === 'last_30d') {
    const s = new Date(today); s.setDate(s.getDate() - 29);
    return { start: isoDate(s), end: isoDate(today) };
  }
  return { start: customStart, end: customEnd };
}

function downloadProductivityCsv(payload) {
  const events = payload.events_visible || [];
  const headerCells = ['User', ...events.map((slug) => EVENT_LABELS[slug]?.title || slug), 'Total'];
  const rows = [headerCells.join(',')];
  for (const u of payload.users || []) {
    const cells = [
      u.display_name || u.username,
      ...events.map((slug) => String(u.metrics[slug] ?? 0)),
      String(u.total ?? 0),
    ];
    rows.push(cells.map((v) => (String(v).includes(',') ? `"${v}"` : v)).join(','));
  }
  const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `productivity-${payload.range?.start || 'today'}_${payload.range?.end || 'today'}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function EventCard({ slug, payload, onExpand, isExpanded }) {
  const total = payload.totals_per_event?.[slug] ?? 0;
  const users = (payload.users || []).filter((u) => (u.metrics?.[slug] ?? 0) > 0);
  users.sort((a, b) => (b.metrics[slug] || 0) - (a.metrics[slug] || 0));
  const top = users[0]?.metrics[slug] || 0;
  const meta = EVENT_LABELS[slug] || { title: slug, unit: '' };

  return (
    <div
      style={styles.card(isExpanded)}
      onClick={onExpand}
      role="button"
      tabIndex={0}
    >
      <div style={styles.cardHeader}>
        <span style={styles.cardTitle}>{meta.title}</span>
        <span style={styles.cardTotal}>{total}</span>
      </div>
      <div style={styles.cardSubheader}>{meta.unit}</div>
      {users.length === 0 ? (
        <div style={styles.cardEmpty}>No data for this range.</div>
      ) : (
        <div style={styles.barChart}>
          {users.map((u, idx) => {
            const value = u.metrics[slug] || 0;
            const ratio = top > 0 ? value / top : 0;
            const color = idx === 0 ? COLOR_TOP : COLOR_OTHER;
            return (
              <div key={u.user_id} style={styles.barRow}>
                <span style={styles.barLabel}>{u.display_name || u.username}</span>
                <div style={styles.barTrack}>
                  <div style={{ ...styles.barFill, width: `${Math.max(2, ratio * 100)}%`, background: color }} />
                </div>
                <span style={styles.barValue}>{value}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ProductivityTable({ payload }) {
  const events = payload.events_visible || [];
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
      <thead>
        <tr style={{ borderBottom: '1px solid var(--border)' }}>
          <th style={{ ...styles.th, textAlign: 'left' }}>User</th>
          {events.map((slug) => (
            <th key={slug} style={{ ...styles.th, textAlign: 'right' }}>
              {EVENT_LABELS[slug]?.title || slug}
            </th>
          ))}
          <th style={{ ...styles.th, textAlign: 'right' }}>Total</th>
        </tr>
      </thead>
      <tbody>
        {(payload.users || []).map((u) => (
          <tr key={u.user_id} style={{ borderBottom: '1px solid var(--border)' }}>
            <td style={styles.td}>{u.display_name || u.username}</td>
            {events.map((slug) => (
              <td key={slug} style={{ ...styles.td, textAlign: 'right' }} className="mono">
                {u.metrics[slug] || 0}
              </td>
            ))}
            <td style={{ ...styles.td, textAlign: 'right', fontWeight: 600 }} className="mono">
              {u.total || 0}
            </td>
          </tr>
        ))}
        {(payload.users || []).length === 0 && (
          <tr>
            <td colSpan={events.length + 2} style={{ ...styles.td, color: 'var(--text-secondary)' }}>
              No data for this range.
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

// avid-overhaul-mk1 P11.2: Dashboard is now a thin tab shell. The
// original productivity view became one of three tabs; the other two
// (Received Today, Shipping Health) live in this file as siblings so
// they share the warehouse context + page header. Tab selection is
// local-only state (no URL persistence yet -- can be added once
// stakeholders show they want shareable deep links).
export default function Dashboard() {
  const { warehouseId } = useWarehouse();
  const [tab, setTab] = useState('productivity');
  return (
    <div>
      <PageHeader title="Dashboard" />
      <div className="data-tabs" style={{ marginBottom: 16 }}>
        <button
          type="button"
          className={`data-tab${tab === 'productivity' ? ' active' : ''}`}
          onClick={() => setTab('productivity')}
        >
          Productivity
        </button>
        <button
          type="button"
          className={`data-tab${tab === 'received' ? ' active' : ''}`}
          onClick={() => setTab('received')}
        >
          Received Today
        </button>
        <button
          type="button"
          className={`data-tab${tab === 'shipping' ? ' active' : ''}`}
          onClick={() => setTab('shipping')}
        >
          Shipping Health
        </button>
      </div>
      {tab === 'productivity' && <ProductivityView warehouseId={warehouseId} />}
      {tab === 'received' && <ReceivedTodayView warehouseId={warehouseId} />}
      {tab === 'shipping' && <ShippingHealthView warehouseId={warehouseId} />}
    </div>
  );
}

function ProductivityView({ warehouseId }) {
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState('');
  const [preferences, setPreferences] = useState({
    chart_order: ['picking', 'packing', 'shipped', 'received_skus', 'putaway_skus'],
    default_range: 'today',
    default_view: 'charts',
  });
  const [rangePreset, setRangePreset] = useState('today');
  const [customStart, setCustomStart] = useState('');
  const [customEnd, setCustomEnd] = useState('');
  const [view, setView] = useState('charts');
  const [expandedSlug, setExpandedSlug] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const [savingPrefs, setSavingPrefs] = useState(false);

  useEffect(() => { loadPreferences(); }, []);

  async function loadPreferences() {
    const res = await api.get('/v1/dashboard/preferences');
    if (res?.ok) {
      const data = await res.json();
      setPreferences(data);
      setRangePreset(data.default_range || 'today');
      setView(data.default_view || 'charts');
    }
  }

  useEffect(() => {
    if (!warehouseId) return;
    if (rangePreset === 'custom' && (!customStart || !customEnd)) return;
    loadProductivity();
  }, [warehouseId, rangePreset, customStart, customEnd]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function loadProductivity() {
    setError('');
    const range = rangeForPreset(rangePreset, customStart, customEnd);
    if (!range.start || !range.end) return;
    const qp = new URLSearchParams({
      start: range.start, end: range.end, warehouse_id: String(warehouseId),
    });
    // P6.1: productivity is ADMIN-only upstream; a USER hitting the
    // home page should not see "Forbidden" inline. Silent the popup
    // and on 403 just clear the payload so the widget shows nothing.
    const res = await api.get(
      `/v1/dashboard/productivity?${qp}`,
      { silentPermissionDenied: true },
    );
    if (res?.ok) {
      setPayload(await res.json());
    } else if (res?.status === 403) {
      setPayload(null);
    } else {
      const data = await res?.json();
      setPayload(null);
      setError(data?.error || 'Failed to load productivity');
    }
  }

  async function savePreferences(patch) {
    setSavingPrefs(true);
    const res = await api.put('/v1/dashboard/preferences', patch);
    if (res?.ok) {
      const data = await res.json();
      setPreferences(data);
    }
    setSavingPrefs(false);
  }

  function reorderChart(slug, direction) {
    const order = [...preferences.chart_order];
    const idx = order.indexOf(slug);
    if (idx < 0) return;
    const swap = idx + direction;
    if (swap < 0 || swap >= order.length) return;
    [order[idx], order[swap]] = [order[swap], order[idx]];
    savePreferences({ chart_order: order });
  }

  // Visible chart order: filter chart_order by events_visible (so
  // packing disappears when require_packing_before_shipping=false),
  // then append any new events not in the user's saved order.
  const visibleSlugs = useMemo(() => {
    const visible = payload?.events_visible || [];
    const ordered = (preferences.chart_order || []).filter((s) => visible.includes(s));
    for (const s of visible) {
      if (!ordered.includes(s)) ordered.push(s);
    }
    return ordered;
  }, [payload, preferences.chart_order]);

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', gap: 4 }}>
          {RANGE_PRESETS.map((p) => (
            <button
              key={p.key}
              className={`btn btn-sm${rangePreset === p.key ? ' btn-primary' : ''}`}
              onClick={() => setRangePreset(p.key)}
            >
              {p.label}
            </button>
          ))}
        </div>
        {rangePreset === 'custom' && (
          <>
            <input
              type="date"
              className="form-input"
              style={{ width: 150 }}
              value={customStart}
              onChange={(e) => setCustomStart(e.target.value)}
            />
            <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>to</span>
            <input
              type="date"
              className="form-input"
              style={{ width: 150 }}
              value={customEnd}
              onChange={(e) => setCustomEnd(e.target.value)}
            />
          </>
        )}
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          <button
            className={`btn btn-sm${view === 'charts' ? ' btn-primary' : ''}`}
            onClick={() => setView('charts')}
          >
            Charts
          </button>
          <button
            className={`btn btn-sm${view === 'table' ? ' btn-primary' : ''}`}
            onClick={() => setView('table')}
          >
            Table
          </button>
          {view === 'table' && payload && (
            <button
              className="btn btn-sm"
              onClick={() => downloadProductivityCsv(payload)}
              title="Download CSV"
            >
              Export CSV
            </button>
          )}
          <button
            className="btn btn-sm"
            onClick={() => setShowSettings(true)}
            title="Dashboard settings"
            aria-label="Dashboard settings"
          >
            &#9881;
          </button>
        </div>
      </div>

      {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}

      {!payload ? (
        <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-secondary)' }}>
          Loading...
        </div>
      ) : view === 'table' ? (
        <ProductivityTable payload={payload} />
      ) : expandedSlug ? (
        <div>
          <button className="btn btn-sm" onClick={() => setExpandedSlug(null)} style={{ marginBottom: 12 }}>
            &larr; Back to grid
          </button>
          <div style={styles.expandedShell}>
            <EventCard
              slug={expandedSlug}
              payload={payload}
              onExpand={() => {}}
              isExpanded
            />
          </div>
        </div>
      ) : (
        <div style={styles.grid}>
          {visibleSlugs.map((slug) => (
            <EventCard
              key={slug}
              slug={slug}
              payload={payload}
              onExpand={() => setExpandedSlug(slug)}
            />
          ))}
        </div>
      )}

      {showSettings && (
        <Modal
          title="Dashboard settings"
          onClose={() => setShowSettings(false)}
          footer={<button className="btn" onClick={() => setShowSettings(false)}>Close</button>}
        >
          <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
            Per-user preferences. Saves on every change. {savingPrefs && '(saving...)'}
          </p>
          <div className="form-group">
            <label>Default range</label>
            <select
              className="form-select"
              value={preferences.default_range}
              onChange={(e) => savePreferences({ default_range: e.target.value })}
            >
              {RANGE_PRESETS.map((p) => (
                <option key={p.key} value={p.key}>{p.label}</option>
              ))}
            </select>
          </div>
          <div className="form-group">
            <label>Default view</label>
            <select
              className="form-select"
              value={preferences.default_view}
              onChange={(e) => savePreferences({ default_view: e.target.value })}
            >
              <option value="charts">Charts</option>
              <option value="table">Table</option>
            </select>
          </div>
          <div className="form-group">
            <label>Chart order</label>
            <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
              {(preferences.chart_order || []).map((slug, idx) => (
                <li
                  key={slug}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 8,
                    padding: '6px 8px',
                    borderBottom: '1px solid var(--border)',
                  }}
                >
                  <span style={{ flex: 1 }}>
                    {EVENT_LABELS[slug]?.title || slug}
                  </span>
                  <button
                    className="btn btn-sm"
                    onClick={() => reorderChart(slug, -1)}
                    disabled={idx === 0}
                    aria-label="Move up"
                  >&#8593;</button>
                  <button
                    className="btn btn-sm"
                    onClick={() => reorderChart(slug, 1)}
                    disabled={idx === preferences.chart_order.length - 1}
                    aria-label="Move down"
                  >&#8595;</button>
                </li>
              ))}
            </ul>
          </div>
        </Modal>
      )}
    </div>
  );
}

const styles = {
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
    gap: 16,
  },
  expandedShell: {
    maxWidth: 800,
  },
  card: (expanded) => ({
    background: 'var(--card-bg, #fff)',
    border: '1px solid var(--border)',
    borderRadius: 8,
    padding: 16,
    cursor: 'pointer',
    minHeight: expanded ? 480 : 220,
  }),
  cardHeader: {
    display: 'flex', alignItems: 'baseline', justifyContent: 'space-between',
    marginBottom: 4,
  },
  cardTitle: { fontSize: 14, fontWeight: 600 },
  cardTotal: { fontSize: 22, fontWeight: 700, color: COLOR_TOP, fontFamily: 'monospace' },
  cardSubheader: { fontSize: 11, color: 'var(--text-secondary)', marginBottom: 12 },
  cardEmpty: { fontSize: 12, color: 'var(--text-secondary)', textAlign: 'center', padding: 20 },
  barChart: { display: 'flex', flexDirection: 'column', gap: 6 },
  barRow: { display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 },
  barLabel: { width: 90, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  barTrack: { flex: 1, height: 14, background: 'var(--border)', borderRadius: 4 },
  barFill: { height: '100%', borderRadius: 4 },
  barValue: { width: 40, textAlign: 'right', fontFamily: 'monospace' },
  th: { padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 },
  td: { padding: '6px 8px' },
};


// ── Received Today (P11.3) ────────────────────────────────────────────────

function ReceivedTodayView({ warehouseId }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!warehouseId) return;
    load();
  }, [warehouseId]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function load() {
    setLoading(true);
    const res = await api.get(
      `/v1/dashboard/received-today?warehouse_id=${warehouseId}`,
      { silentPermissionDenied: true },
    );
    setLoading(false);
    if (!res?.ok) return;
    const data = await res.json();
    setRows(data.pos || []);
  }

  const totalUnits = rows.reduce((acc, r) => acc + (r.units_received || 0), 0);
  const totalLines = rows.reduce((acc, r) => acc + (r.lines_received || 0), 0);
  const distinctReceivers = new Set(rows.flatMap((r) => r.receivers || []));

  return (
    <div>
      <div style={{
        display: 'flex', gap: 16, marginBottom: 16, fontSize: 13,
        color: 'var(--text-secondary)', flexWrap: 'wrap', alignItems: 'center',
      }}>
        <span><strong style={{ color: 'var(--text)' }}>{rows.length}</strong> POs received today</span>
        <span><strong style={{ color: 'var(--text)' }}>{totalLines}</strong> lines</span>
        <span><strong style={{ color: 'var(--text)' }}>{totalUnits}</strong> units</span>
        <span><strong style={{ color: 'var(--text)' }}>{distinctReceivers.size}</strong> receivers active</span>
        <button className="btn btn-sm" onClick={load} disabled={loading} style={{ marginLeft: 'auto' }}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {!loading && rows.length === 0 && (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          No POs have received units today.
        </p>
      )}

      {rows.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>PO #</th>
              <th>Vendor</th>
              <th>Status</th>
              <th style={{ textAlign: 'right' }}>Lines</th>
              <th style={{ textAlign: 'right' }}>Units</th>
              <th>Receiver(s)</th>
              <th>Last received</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.po_id}>
                <td className="mono">{r.po_number}</td>
                <td>{r.vendor_name || '-'}</td>
                <td>{r.status}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.lines_received}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.units_received}</td>
                <td>{(r.receivers || []).join(', ') || '-'}</td>
                <td className="mono" style={{ fontSize: 12 }}>
                  {r.last_received_at ? new Date(r.last_received_at).toLocaleTimeString() : '-'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}


// ── Shipping Health (P11.4) ───────────────────────────────────────────────

function ShippingHealthView({ warehouseId }) {
  const [data, setData] = useState({ by_source: [], stuck_orders: [], stuck_threshold_days: 8 });
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!warehouseId) return;
    load();
  }, [warehouseId]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function load() {
    setLoading(true);
    const res = await api.get(
      `/v1/dashboard/shipping-health?warehouse_id=${warehouseId}`,
      { silentPermissionDenied: true },
    );
    setLoading(false);
    if (!res?.ok) return;
    setData(await res.json());
  }

  function formatAge(iso) {
    if (!iso) return '-';
    const ms = Date.now() - new Date(iso).getTime();
    const days = ms / (1000 * 60 * 60 * 24);
    if (days < 1) return `${Math.round(days * 24)}h`;
    return `${days.toFixed(1)}d`;
  }

  return (
    <div>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        alignItems: 'center', marginBottom: 12,
      }}>
        <h3 style={{ fontSize: 14, fontWeight: 600, margin: 0 }}>By source system</h3>
        <button className="btn btn-sm" onClick={load} disabled={loading}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {(data.by_source || []).length === 0 && !loading && (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          No sales orders in this warehouse yet.
        </p>
      )}

      {(data.by_source || []).length > 0 && (
        <table className="data-table" style={{ marginBottom: 24 }}>
          <thead>
            <tr>
              <th>Source</th>
              <th style={{ textAlign: 'right' }}>Open</th>
              <th style={{ textAlign: 'right' }}>Picking</th>
              <th style={{ textAlign: 'right' }}>Picked</th>
              <th style={{ textAlign: 'right' }}>Packing</th>
              <th style={{ textAlign: 'right' }}>Packed</th>
              <th style={{ textAlign: 'right' }}>Shipped today</th>
              <th style={{ textAlign: 'right' }}>Unshipped</th>
              <th>Oldest age</th>
            </tr>
          </thead>
          <tbody>
            {data.by_source.map((r) => (
              <tr key={r.source_system}>
                <td className="mono">{r.source_system}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.open}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.picking}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.picked}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.packing}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.packed}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.shipped_today}</td>
                <td className="mono" style={{ textAlign: 'right', fontWeight: r.unshipped_count > 0 ? 600 : 400 }}>
                  {r.unshipped_count}
                </td>
                <td className="mono" style={{ fontSize: 12 }}>{formatAge(r.oldest_unshipped_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3 style={{ fontSize: 14, fontWeight: 600, margin: '0 0 8px 0' }}>
        Stuck orders
        <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--text-secondary)', marginLeft: 8 }}>
          past ship-by date or older than {data.stuck_threshold_days || 8} days
        </span>
      </h3>

      {(data.stuck_orders || []).length === 0 && !loading && (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
          Nothing stuck. Outbound is current.
        </p>
      )}

      {(data.stuck_orders || []).length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>SO #</th>
              <th>Customer</th>
              <th>Source</th>
              <th>Status</th>
              <th>Created</th>
              <th>Ship by</th>
              <th style={{ textAlign: 'right' }}>Age (days)</th>
            </tr>
          </thead>
          <tbody>
            {data.stuck_orders.map((s) => (
              <tr key={s.so_id}>
                <td className="mono">{s.so_number}</td>
                <td>{s.customer_name || '-'}</td>
                <td className="mono">{s.source_system}</td>
                <td>{s.status}</td>
                <td className="mono" style={{ fontSize: 12 }}>
                  {s.created_at ? new Date(s.created_at).toLocaleDateString() : '-'}
                </td>
                <td className="mono" style={{ fontSize: 12, color: s.ship_by_date && new Date(s.ship_by_date) < new Date() ? 'var(--accent)' : undefined }}>
                  {s.ship_by_date || '-'}
                </td>
                <td className="mono" style={{ textAlign: 'right' }}>{s.age_days}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
