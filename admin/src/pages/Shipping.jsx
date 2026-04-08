import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';

export default function Shipping() {
  const [orders, setOrders] = useState([]);
  const [shipped, setShipped] = useState([]);

  useEffect(() => {
    async function load() {
      // Fetch packing toggle to determine which statuses are "ready to ship"
      let requirePacking = true;
      try {
        const settingRes = await api.get('/admin/settings/require_packing_before_shipping');
        if (settingRes?.ok) {
          const data = await settingRes.json();
          requirePacking = data.value !== 'false' && data.value !== false;
        }
      } catch {}

      const statuses = requirePacking ? ['PACKED'] : ['PICKED', 'PACKED'];
      const fetches = statuses.map((s) =>
        api.get(`/admin/sales-orders?status=${s}&warehouse_id=1&per_page=50`)
      );
      const results = await Promise.all(fetches);
      const all = [];
      for (const res of results) {
        if (res?.ok) {
          const data = await res.json();
          all.push(...(data.sales_orders || []));
        }
      }
      setOrders(all);

      const shippedRes = await api.get('/admin/sales-orders?status=SHIPPED&warehouse_id=1&per_page=20');
      if (shippedRes?.ok) {
        const data = await shippedRes.json();
        setShipped(data.sales_orders || []);
      }
    }
    load();
  }, []);

  const columns = [
    { key: 'so_number', label: 'SO Number', mono: true },
    { key: 'customer_name', label: 'Customer' },
    { key: 'lines', label: 'Items', render: (r) => r.lines?.length ?? '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'ship_method', label: 'Ship Method', render: (r) => r.ship_method || '-' },
  ];

  const shippedColumns = [
    { key: 'so_number', label: 'SO Number', mono: true },
    { key: 'customer_name', label: 'Customer' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'carrier', label: 'Carrier', render: (r) => r.carrier || '-' },
    { key: 'tracking_number', label: 'Tracking', mono: true, render: (r) => r.tracking_number || '-' },
    { key: 'shipped_at', label: 'Shipped', mono: true, render: (r) => r.shipped_at ? new Date(r.shipped_at).toLocaleDateString() : '-' },
  ];

  return (
    <div>
      <PageHeader title="Shipping" />
      <div className="section">
        <div className="section-title">Ready to ship</div>
        <DataTable columns={columns} data={orders} emptyMessage="No orders waiting to ship" />
      </div>
      {shipped.length > 0 && (
        <div className="section">
          <div className="section-title">Recently shipped</div>
          <DataTable columns={shippedColumns} data={shipped} />
        </div>
      )}
    </div>
  );
}
