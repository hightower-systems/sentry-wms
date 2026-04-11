import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';

export default function PutAway() {
  const { warehouseId } = useWarehouse();
  const [items, setItems] = useState([]);

  useEffect(() => {
    if (!warehouseId) return;
    api.get(`/putaway/pending/${warehouseId}`).then(async (res) => {
      if (!res?.ok) return;
      const data = await res.json();
      setItems(data.pending_items || []);
    });
  }, [warehouseId]);

  const columns = [
    { key: 'sku', label: 'SKU', mono: true },
    { key: 'item_name', label: 'Item Name' },
    { key: 'quantity', label: 'Qty in Staging', render: (r) => r.quantity_on_hand || r.quantity || '-' },
    { key: 'bin_code', label: 'Bin Code', mono: true },
    { key: 'suggested_bin', label: 'Suggested Bin', mono: true, render: (r) => r.suggested_bin || r.default_bin_code || '-' },
  ];

  return (
    <div>
      <PageHeader title="Put-Away" />
      <DataTable columns={columns} data={items} emptyMessage="No items awaiting put-away" />
    </div>
  );
}
