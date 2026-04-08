import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';

export default function Packing() {
  const [orders, setOrders] = useState([]);
  const [packingEnabled, setPackingEnabled] = useState(null);

  useEffect(() => {
    api.get('/admin/settings/require_packing_before_shipping').then(async (res) => {
      if (!res?.ok) return;
      const data = await res.json();
      const enabled = data.value !== 'false' && data.value !== false;
      setPackingEnabled(enabled);
      if (enabled) {
        const soRes = await api.get('/admin/sales-orders?status=PICKED&warehouse_id=1&per_page=50');
        if (soRes?.ok) {
          const soData = await soRes.json();
          setOrders(soData.sales_orders || []);
        }
      }
    }).catch(() => setPackingEnabled(true));
  }, []);

  const columns = [
    { key: 'order_number', label: 'SO Number', mono: true },
    { key: 'customer_name', label: 'Customer' },
    { key: 'lines', label: 'Items', render: (r) => r.lines?.length ?? '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
  ];

  if (packingEnabled === false) {
    return (
      <div>
        <PageHeader title="Packing" />
        <div className="empty-state" style={{ textAlign: 'center', padding: '60px 20px' }}>
          <h2 style={{ marginBottom: 8 }}>Packing is not enabled</h2>
          <p style={{ color: 'var(--text-secondary)', marginBottom: 16 }}>
            Enable "Require Packing before Shipping" in Settings to use this page.
          </p>
          <Link to="/settings" className="btn btn-primary">Go to Settings</Link>
        </div>
      </div>
    );
  }

  return (
    <div>
      <PageHeader title="Packing" />
      <DataTable columns={columns} data={orders} emptyMessage="No orders waiting to pack" />
    </div>
  );
}
