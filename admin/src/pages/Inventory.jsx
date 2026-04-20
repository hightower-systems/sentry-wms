import { useState, useEffect, useMemo } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';

export default function Inventory() {
  const { warehouseId } = useWarehouse();
  const [data, setData] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState(null);
  const [sortDir, setSortDir] = useState('asc');

  useEffect(() => {
    if (!warehouseId) return;
    const params = new URLSearchParams({ warehouse_id: warehouseId, page, per_page: 50 });
    if (search) params.set('q', search);
    api.get(`/admin/inventory?${params}`).then(async (res) => {
      if (!res?.ok) return;
      const json = await res.json();
      setData(json.inventory || []);
      setPagination({ page: json.page, pages: json.pages, total: json.total, per_page: json.per_page });
    });
  }, [page, search, warehouseId]);

  function commitSearch() {
    setSearch(searchInput);
    setPage(1);
  }

  function handleSort(key) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }

  const sorted = useMemo(() => {
    if (!sortKey) return data;
    return [...data].sort((a, b) => {
      let av = a[sortKey], bv = b[sortKey];
      if (av == null) av = '';
      if (bv == null) bv = '';
      if (typeof av === 'number' && typeof bv === 'number') {
        return sortDir === 'asc' ? av - bv : bv - av;
      }
      const cmp = String(av).localeCompare(String(bv), undefined, { numeric: true });
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [data, sortKey, sortDir]);

  const columns = [
    { key: 'sku', label: 'SKU', mono: true, sortable: true },
    { key: 'item_name', label: 'Item Name', sortable: true },
    { key: 'bin_code', label: 'Bin Code', mono: true, sortable: true },
    { key: 'zone_name', label: 'Zone', sortable: true },
    { key: 'quantity_on_hand', label: 'On Hand', sortable: true },
    { key: 'available', label: 'Available', sortable: true, render: (r) => (r.quantity_on_hand || 0) - (r.committed_to_orders || 0) },
    { key: 'last_counted_at', label: 'Last Counted', mono: true, sortable: true, render: (r) => r.last_counted_at ? new Date(r.last_counted_at).toLocaleDateString() : '-' },
  ];

  return (
    <div>
      <PageHeader title="Inventory" />
      <div className="filter-bar">
        <input
          className="form-input"
          placeholder="Search by SKU or item name (press Enter)"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') commitSearch(); }}
          onBlur={commitSearch}
        />
      </div>
      <DataTable
        columns={columns}
        data={sorted}
        pagination={pagination}
        onPageChange={setPage}
        sortKey={sortKey}
        sortDir={sortDir}
        onSort={handleSort}
      />
    </div>
  );
}
