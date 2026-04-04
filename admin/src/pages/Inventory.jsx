import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';

export default function Inventory() {
  const [data, setData] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');

  useEffect(() => {
    const params = new URLSearchParams({ warehouse_id: 1, page, per_page: 50 });
    if (search) params.set('q', search);
    api.get(`/admin/inventory?${params}`).then(async (res) => {
      if (!res?.ok) return;
      const json = await res.json();
      setData(json.inventory || []);
      setPagination({ page: json.page, pages: json.pages, total: json.total, per_page: json.per_page });
    });
  }, [page, search]);

  const columns = [
    { key: 'sku', label: 'SKU', mono: true },
    { key: 'item_name', label: 'Item Name' },
    { key: 'bin_code', label: 'Bin Code', mono: true },
    { key: 'zone_name', label: 'Zone' },
    { key: 'quantity_on_hand', label: 'On Hand' },
    { key: 'quantity_allocated', label: 'Allocated' },
    { key: 'available', label: 'Available', render: (r) => (r.quantity_on_hand || 0) - (r.quantity_allocated || 0) },
    { key: 'last_counted_at', label: 'Last Counted', mono: true, render: (r) => r.last_counted_at ? new Date(r.last_counted_at).toLocaleDateString() : '-' },
  ];

  return (
    <div>
      <PageHeader title="Inventory" />
      <div className="filter-bar">
        <input
          className="form-input"
          placeholder="Search by SKU or item name..."
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
        />
      </div>
      <DataTable
        columns={columns}
        data={data}
        pagination={pagination}
        onPageChange={setPage}
      />
    </div>
  );
}
