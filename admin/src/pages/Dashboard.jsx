import { useState, useEffect } from 'react';
import { api } from '../api.js';
import Pipeline from '../components/Pipeline.jsx';
import DataTable from '../components/DataTable.jsx';
import StatusTag from '../components/StatusTag.jsx';
import PageHeader from '../components/PageHeader.jsx';

export default function Dashboard() {
  const [stats, setStats] = useState(null);
  const [orders, setOrders] = useState([]);
  const [pos, setPos] = useState([]);
  const [shortPicks, setShortPicks] = useState([]);

  useEffect(() => {
    api.get('/admin/dashboard?warehouse_id=1').then(async (res) => {
      if (!res?.ok) return;
      setStats(await res.json());
    });

    Promise.all([
      api.get('/admin/sales-orders?status=OPEN&warehouse_id=1&per_page=50'),
      api.get('/admin/sales-orders?status=ALLOCATED&warehouse_id=1&per_page=50'),
      api.get('/admin/sales-orders?status=PICKING&warehouse_id=1&per_page=50'),
    ]).then(async (responses) => {
      const all = [];
      for (const res of responses) {
        if (res?.ok) {
          const data = await res.json();
          all.push(...(data.sales_orders || []));
        }
      }
      setOrders(all);
    });

    api.get('/admin/short-picks?warehouse_id=1&days=7').then(async (res) => {
      if (res?.ok) {
        const data = await res.json();
        setShortPicks(data.short_picks || []);
      }
    }).catch(() => {});

    Promise.all([
      api.get('/admin/purchase-orders?status=OPEN&warehouse_id=1&per_page=50'),
      api.get('/admin/purchase-orders?status=PARTIAL&warehouse_id=1&per_page=50'),
    ]).then(async (responses) => {
      const all = [];
      for (const res of responses) {
        if (res?.ok) {
          const data = await res.json();
          all.push(...(data.purchase_orders || []));
        }
      }
      setPos(all);
    });
  }, []);

  const pipeline = stats
    ? [
        { label: 'To Receive', count: stats.open_pos || 0, color: 'blue' },
        { label: 'Put-away', count: stats.pending_putaway || 0, color: '' },
        { label: 'To Pick', count: stats.orders_ready_to_pick || 0, color: 'green' },
        ...(stats.require_packing ? [
          { label: 'To Pack', count: stats.ready_to_pack || 0, color: '' },
          { label: 'Packed', count: stats.orders_packed || 0, color: 'purple' },
        ] : []),
        { label: 'To Ship', count: stats.ready_to_ship || 0, color: 'purple' },
        { label: 'Low Stock', count: stats.low_stock_items || 0, color: 'red' },
        { label: 'Short Picks (7d)', count: stats.short_picks_7d || 0, color: stats.short_picks_7d > 0 ? 'red' : '' },
      ]
    : [];

  const orderCols = [
    { key: 'order_number', label: 'Order', mono: true },
    { key: 'customer_name', label: 'Customer' },
    { key: 'line_count', label: 'Lines', render: (r) => r.lines?.length ?? r.line_count ?? '-' },
    { key: 'total_qty', label: 'Qty', render: (r) => r.lines?.reduce((s, l) => s + l.quantity, 0) ?? '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'ship_by', label: 'Ship by', mono: true, render: (r) => r.ship_by || '-' },
  ];

  const poCols = [
    { key: 'po_number', label: 'PO', mono: true },
    { key: 'vendor_name', label: 'Vendor' },
    { key: 'line_count', label: 'Lines', render: (r) => r.lines?.length ?? r.line_count ?? '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'expected_date', label: 'Expected', mono: true, render: (r) => r.expected_date || '-' },
  ];

  return (
    <div>
      <PageHeader title="Dashboard" />

      {pipeline.length > 0 && <Pipeline items={pipeline} />}

      <div className="section">
        <div className="section-title">Orders needing warehouse action</div>
        <DataTable columns={orderCols} data={orders} />
      </div>

      <div className="grid-2 section">
        <div className="card">
          <div className="card-title">Low stock alerts</div>
          {stats?.low_stock_items?.length > 0 ? (
            stats.low_stock_items.map((item, i) => (
              <div key={i} className="low-stock-item">
                <span>
                  <span className="low-stock-sku">{item.sku}</span>{' '}
                  <span style={{ color: 'var(--text-secondary)' }}>{item.item_name}</span>
                </span>
                <span className="low-stock-qty">{item.on_hand} on hand</span>
              </div>
            ))
          ) : (
            <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>No low stock alerts</div>
          )}
        </div>
        <div className="card">
          <div className="card-title">Recent activity</div>
          {stats?.recent_activity?.length > 0 ? (
            <ul className="activity-list">
              {stats.recent_activity.map((a, i) => (
                <li key={i} className="activity-item">
                  <span className="activity-time">{new Date(a.created_at).toLocaleString()}</span>{' '}
                  <span className="activity-action">
                    {a.action_type} {a.entity_type ? `on ${a.entity_type}` : ''}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>No recent activity</div>
          )}
        </div>
      </div>

      {shortPicks.length > 0 && (
        <div className="section">
          <div className="section-title">Short picks (last 7 days)</div>
          <DataTable columns={[
            { key: 'sku', label: 'SKU', mono: true },
            { key: 'bin_code', label: 'Bin', mono: true },
            { key: 'qty_expected', label: 'Expected' },
            { key: 'qty_picked', label: 'Picked' },
            { key: 'shortage', label: 'Short', render: (r) => <span style={{ color: 'var(--accent-red)' }}>{r.shortage}</span> },
            { key: 'user', label: 'Picker' },
            { key: 'timestamp', label: 'When', mono: true, render: (r) => r.timestamp ? new Date(r.timestamp).toLocaleString() : '-' },
          ]} data={shortPicks} />
        </div>
      )}

      <div className="section">
        <div className="section-title">Inbound expected</div>
        <DataTable columns={poCols} data={pos} />
      </div>
    </div>
  );
}
