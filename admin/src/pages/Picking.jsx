import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';

export default function Picking() {
  const [orders, setOrders] = useState([]);

  useEffect(() => {
    Promise.all([
      api.get('/admin/sales-orders?status=OPEN&warehouse_id=1&per_page=50'),
      api.get('/admin/sales-orders?status=ALLOCATED&warehouse_id=1&per_page=50'),
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
  }, []);

  const columns = [
    { key: 'order_number', label: 'SO Number', mono: true },
    { key: 'customer_name', label: 'Customer' },
    { key: 'lines', label: 'Lines', render: (r) => r.lines?.length ?? r.line_count ?? '-' },
    { key: 'total_qty', label: 'Qty', render: (r) => r.lines?.reduce((s, l) => s + l.quantity, 0) ?? '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'ship_by', label: 'Ship by', mono: true, render: (r) => r.ship_by || '-' },
  ];

  return (
    <div>
      <PageHeader title="Picking" />
      <div className="section">
        <div className="section-title">Orders ready to pick</div>
        <DataTable columns={columns} data={orders} emptyMessage="No orders ready for picking" />
      </div>
    </div>
  );
}
