import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';

export default function Shipping() {
  const [orders, setOrders] = useState([]);

  useEffect(() => {
    api.get('/admin/sales-orders?status=PACKED&warehouse_id=1&per_page=50').then(async (res) => {
      if (!res?.ok) return;
      const data = await res.json();
      setOrders(data.sales_orders || []);
    });
  }, []);

  const columns = [
    { key: 'order_number', label: 'SO Number', mono: true },
    { key: 'customer_name', label: 'Customer' },
    { key: 'lines', label: 'Items', render: (r) => r.lines?.length ?? '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'ship_method', label: 'Ship Method', render: (r) => r.ship_method || '-' },
  ];

  return (
    <div>
      <PageHeader title="Shipping" />
      <DataTable columns={columns} data={orders} emptyMessage="No orders waiting to ship" />
    </div>
  );
}
