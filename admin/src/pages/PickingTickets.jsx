import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';

// Statuses that still have something useful to put on a printed
// picking ticket. SHIPPED/CANCELLED orders are skipped from the queue
// so we don't accidentally hand a picker a slip for a done order.
const PICKABLE_STATUSES = ['OPEN', 'ALLOCATED', 'PICKING', 'PICKED'];

export default function PickingTickets() {
  const navigate = useNavigate();
  const { warehouseId } = useWarehouse();
  const [orders, setOrders] = useState([]);
  const [search, setSearch] = useState('');
  const [lookupError, setLookupError] = useState('');

  useEffect(() => {
    if (!warehouseId) return;
    let cancelled = false;
    (async () => {
      const responses = await Promise.all(
        PICKABLE_STATUSES.map((status) =>
          api.get(`/admin/sales-orders?status=${status}&warehouse_id=${warehouseId}&per_page=50`),
        ),
      );
      const all = [];
      for (const res of responses) {
        if (res?.ok) {
          const data = await res.json();
          all.push(...(data.sales_orders || []));
        }
      }
      if (!cancelled) setOrders(all);
    })();
    return () => { cancelled = true; };
  }, [warehouseId]);

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
        <div className="section-title">Orders ready to pick</div>
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
