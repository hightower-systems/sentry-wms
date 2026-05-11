import { useEffect, useState } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';

// POS Activity dashboard: monitor in-store retail POS sales orders +
// daily KPIs. Sentry stores POS-specific facts (cashier_id, terminal_id,
// total_cents, payment_method) in audit_log.details JSONB rather than
// dedicated columns (v1.10.0 design: pricing stays out of Sentry, lives
// in audit_log for archival). The /api/admin/pos/* backend endpoints
// surface those facts back to this page via LEFT JOIN audit_log.

const STATUS_OPTIONS = ['', 'OPEN', 'ALLOCATED', 'PICKED', 'PACKED', 'SHIPPED', 'CANCELLED'];
const ORDER_TYPE_OPTIONS = ['sale', 'refund', 'all'];

function centsToUsd(cents) {
  if (cents == null) return '-';
  return `$${(cents / 100).toFixed(2)}`;
}

function formatTs(iso) {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

export default function POSActivity() {
  const [summary, setSummary] = useState(null);
  const [salesOrders, setSalesOrders] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [orderType, setOrderType] = useState('sale');
  const [status, setStatus] = useState('');
  const [terminalFilter, setTerminalFilter] = useState('');
  const [cashierFilter, setCashierFilter] = useState('');
  const [loading, setLoading] = useState(false);

  // Refresh summary every load + after page/filter changes that imply new data.
  // Cheap — single SQL query w/ 1-day window.
  function loadSummary() {
    api.get('/admin/pos/summary').then(async (res) => {
      if (!res?.ok) return;
      const data = await res.json();
      setSummary(data);
    });
  }

  function loadOrders() {
    const params = new URLSearchParams({ page, per_page: 50, order_type: orderType });
    if (status) params.set('status', status);
    if (terminalFilter) params.set('terminal_id', terminalFilter);
    if (cashierFilter) params.set('cashier_id', cashierFilter);
    setLoading(true);
    api.get(`/admin/pos/sales-orders?${params}`).then(async (res) => {
      setLoading(false);
      if (!res?.ok) return;
      const data = await res.json();
      setSalesOrders(data.sales_orders || []);
      setPagination({ page: data.page, pages: data.pages, total: data.total, per_page: data.per_page });
    });
  }

  useEffect(() => { loadSummary(); }, []);
  useEffect(() => { loadOrders(); }, [page, orderType, status, terminalFilter, cashierFilter]);

  function refresh() {
    loadSummary();
    loadOrders();
  }

  const columns = [
    { key: 'so_number', label: 'SO #', mono: true },
    { key: 'order_date', label: 'Date', render: (r) => formatTs(r.order_date) },
    { key: 'order_type', label: 'Type' },
    { key: 'status', label: 'Status' },
    { key: 'cashier_id', label: 'Cashier', render: (r) => r.cashier_id || '-' },
    { key: 'terminal_id', label: 'Terminal', mono: true, render: (r) => r.terminal_id || '-' },
    { key: 'customer_name', label: 'Customer', render: (r) => r.customer_name || '(walk-in)' },
    { key: 'total_cents', label: 'Total', render: (r) => centsToUsd(r.total_cents) },
    { key: 'payment_method', label: 'Tender', render: (r) => r.payment_method || '-' },
    { key: 'external_txn_ref', label: 'Windcave Ref', mono: true, render: (r) => r.external_txn_ref || '-' },
  ];

  const today = summary?.today || {};
  const terminals = summary?.active_terminals || [];

  return (
    <div>
      <PageHeader title="POS Activity">
        <button className="btn" onClick={refresh}>Refresh</button>
      </PageHeader>

      {/* KPI Bar */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: 12,
        marginBottom: 16,
      }}>
        <KpiCard
          label="Today's Sales"
          value={today.sales_count ?? '—'}
          sub={centsToUsd(today.sales_total_cents)}
        />
        <KpiCard
          label="Today's Refunds"
          value={today.refund_count ?? '—'}
          sub={centsToUsd(today.refund_total_cents)}
        />
        <KpiCard
          label="Net Revenue Today"
          value={centsToUsd((today.sales_total_cents || 0) - (today.refund_total_cents || 0))}
          sub={`${today.sales_count || 0} sales − ${today.refund_count || 0} refunds`}
        />
        <KpiCard
          label="Active Terminals (24h)"
          value={terminals.length || '—'}
          sub={terminals.join(', ') || 'none'}
        />
      </div>

      {/* Filter bar */}
      <div className="filter-bar" style={{ marginBottom: 12 }}>
        <select
          className="form-select"
          value={orderType}
          onChange={(e) => { setOrderType(e.target.value); setPage(1); }}
          style={{ width: 'auto' }}
        >
          {ORDER_TYPE_OPTIONS.map((v) => (
            <option key={v} value={v}>{v === 'all' ? 'All Types' : (v[0].toUpperCase() + v.slice(1))}</option>
          ))}
        </select>
        <select
          className="form-select"
          value={status}
          onChange={(e) => { setStatus(e.target.value); setPage(1); }}
          style={{ width: 'auto' }}
        >
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>{s || 'All Statuses'}</option>
          ))}
        </select>
        <input
          className="form-input"
          placeholder="Terminal ID (e.g., REG-01)"
          value={terminalFilter}
          onChange={(e) => { setTerminalFilter(e.target.value); setPage(1); }}
          style={{ maxWidth: 200 }}
        />
        <input
          className="form-input"
          placeholder="Cashier ID"
          value={cashierFilter}
          onChange={(e) => { setCashierFilter(e.target.value); setPage(1); }}
          style={{ maxWidth: 200 }}
        />
      </div>

      <DataTable
        columns={columns}
        data={salesOrders}
        pagination={pagination}
        onPageChange={setPage}
        emptyMessage={loading ? 'Loading…' : 'No POS orders match the current filters.'}
      />
    </div>
  );
}

function KpiCard({ label, value, sub }) {
  return (
    <div style={{
      background: 'var(--card-bg, #ffffff)',
      border: '1px solid var(--border, #e5e7eb)',
      borderRadius: 8,
      padding: '12px 16px',
    }}>
      <div style={{
        fontSize: 11,
        textTransform: 'uppercase',
        letterSpacing: 0.5,
        color: 'var(--text-secondary, #6b7280)',
        marginBottom: 4,
      }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 600, marginBottom: 2 }}>{value}</div>
      <div style={{ fontSize: 12, color: 'var(--text-secondary, #6b7280)' }}>{sub}</div>
    </div>
  );
}
