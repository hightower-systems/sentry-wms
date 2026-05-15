import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';

// Statuses that still have something useful to put on a printed
// picking ticket. SHIPPED/CANCELLED orders are skipped from the
// switcher so we don't accidentally hand a picker a slip for a done
// order. OPEN is the default because that's what the warehouse pulls
// from first thing in the morning.
const PICKABLE_STATUSES = ['OPEN', 'ALLOCATED', 'PICKING', 'PICKED'];
const STATUS_OPTIONS = [...PICKABLE_STATUSES, 'ALL'];

export default function PickingTickets() {
  const navigate = useNavigate();
  const { warehouseId } = useWarehouse();
  const [status, setStatus] = useState('OPEN');
  const [orders, setOrders] = useState([]);
  const [search, setSearch] = useState('');
  const [lookupError, setLookupError] = useState('');
  // Bump this to force a refetch of the list under the current filters.
  // Lets a manual Refresh button pull fresh sales-order data without
  // requiring the user to navigate away and back (e.g. after the daily
  // Amazon name+address backfill push updates customer details on
  // already-pushed SOs).
  const [refreshCounter, setRefreshCounter] = useState(0);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (!warehouseId) return;
    let cancelled = false;
    setRefreshing(true);
    (async () => {
      const statuses = status === 'ALL' ? PICKABLE_STATUSES : [status];
      const responses = await Promise.all(
        statuses.map((s) =>
          api.get(`/admin/sales-orders?status=${s}&warehouse_id=${warehouseId}&per_page=50`),
        ),
      );
      const all = [];
      for (const res of responses) {
        if (res?.ok) {
          const data = await res.json();
          all.push(...(data.sales_orders || []));
        }
      }
      if (!cancelled) {
        setOrders(all);
        setRefreshing(false);
      }
    })();
    return () => { cancelled = true; };
  }, [warehouseId, status, refreshCounter]);

  async function openTicket() {
    const term = search.trim();
    if (!term) return;
    setLookupError('');
    // Reuse the existing list endpoint so we go through the same
    // auth/role gate as everything else; the detail endpoint takes an
    // so_id (int), not so_number, so we resolve the number first.
    const res = await api.get(
      `/admin/sales-orders?q=${encodeURIComponent(term)}&per_page=10`,
    );
    if (!res?.ok) {
      setLookupError('Could not search sales orders.');
      return;
    }
    const data = await res.json();
    const matches = data.sales_orders || [];
    const exact = matches.find((o) => o.so_number === term) || matches[0];
    if (!exact) {
      setLookupError(`No sales order found matching "${term}".`);
      return;
    }
    navigate(`/picking-tickets/${exact.so_id}/print`);
  }

  function onSearchKey(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      openTicket();
    }
  }

  function printAll() {
    const qs = new URLSearchParams({ status });
    if (warehouseId) qs.set('warehouse_id', String(warehouseId));
    // Open in a new tab so the print queue renders standalone (no
    // admin Layout chrome) and the user can leave it open while
    // continuing to work in the original tab.
    window.open(`/picking-tickets/print-all?${qs.toString()}`, '_blank', 'noopener');
  }

  const columns = [
    { key: 'so_number', label: 'SO Number', mono: true },
    { key: 'customer_name', label: 'Customer' },
    {
      key: 'ship_by_date',
      label: 'Ship By',
      mono: true,
      render: (r) => (r.ship_by_date ? new Date(r.ship_by_date).toLocaleDateString() : '-'),
    },
    { key: 'ship_method', label: 'Ship Method', render: (r) => r.ship_method || '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    {
      key: 'actions',
      label: '',
      render: (r) => (
        <button
          className="btn btn-sm btn-primary"
          onClick={(e) => {
            e.stopPropagation();
            navigate(`/picking-tickets/${r.so_id}/print`);
          }}
        >Print Ticket</button>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title="Picking Tickets" />

      <div className="section">
        <div className="section-title">Find a ticket</div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', maxWidth: 520 }}>
          <input
            className="form-input"
            style={{ flex: 1 }}
            placeholder="Sales order number, e.g. 648415"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={onSearchKey}
          />
          <button className="btn btn-primary" onClick={openTicket}>Open</button>
        </div>
        {lookupError && (
          <div className="form-error" style={{ marginTop: 8 }}>{lookupError}</div>
        )}
      </div>

      <div className="section">
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 12,
            marginBottom: 8,
          }}
        >
          <div className="section-title" style={{ marginBottom: 0 }}>Orders ready to pick</div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <label style={{ fontSize: 13, color: '#555' }}>Status</label>
            <select
              className="form-input"
              style={{ width: 'auto' }}
              value={status}
              onChange={(e) => setStatus(e.target.value)}
            >
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            <button
              className="btn btn-secondary"
              onClick={() => setRefreshCounter((c) => c + 1)}
              disabled={refreshing}
              title="Re-fetch the list from the server (e.g. to pick up just-pushed customer name + shipping address)"
            >
              {refreshing ? 'Refreshing…' : 'Refresh'}
            </button>
            <button
              className="btn btn-primary"
              onClick={printAll}
              disabled={orders.length === 0}
            >
              Print All ({orders.length})
            </button>
          </div>
        </div>
        <DataTable
          columns={columns}
          data={orders}
          emptyMessage="No orders ready for picking"
          onRowClick={(r) => navigate(`/picking-tickets/${r.so_id}/print`)}
        />
      </div>
    </div>
  );
}
