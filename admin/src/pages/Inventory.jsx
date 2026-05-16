import { useState, useEffect, useMemo } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';

// Page-level warehouse + bin filters were added on 2026-05-10 because the
// global topbar warehouse-picker was unreliable for some users (couldn't
// click into the dropdown), making cross-warehouse inventory inspection
// impossible. The page-level filters seed from the topbar context (so
// existing behavior is unchanged for users who were using the topbar) but
// can be independently changed without affecting global state. Bin filter
// is new — the underlying API endpoint already supports bin_id query
// param; the UI just never exposed it.

export default function Inventory() {
  const { warehouseId: topbarWarehouseId } = useWarehouse();
  const [warehouses, setWarehouses] = useState([]);
  const [bins, setBins] = useState([]);
  const [warehouseFilter, setWarehouseFilter] = useState(topbarWarehouseId || null);
  const [binFilter, setBinFilter] = useState(null);
  const [data, setData] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [sortKey, setSortKey] = useState(null);
  const [sortDir, setSortDir] = useState('asc');
  const [loading, setLoading] = useState(false);

  // Load all warehouses once for the filter dropdown. Independent of topbar
  // context so the page-level filter works even if topbar picker doesn't.
  useEffect(() => {
    api.get('/admin/warehouses').then(async (res) => {
      if (!res?.ok) return;
      const json = await res.json();
      const list = json.warehouses || [];
      setWarehouses(list);
      // If the topbar didn't set a warehouseId but warehouses are available,
      // default to the first one so the inventory query has a target.
      if (!warehouseFilter && list.length > 0) {
        const firstId = list[0].warehouse_id || list[0].id;
        setWarehouseFilter(firstId);
      }
    });
  }, []);

  // Load bins for the selected warehouse so the bin-filter dropdown is
  // scoped (avoids showing bins from other warehouses that don't apply).
  useEffect(() => {
    if (!warehouseFilter) {
      setBins([]);
      setBinFilter(null);
      return;
    }
    const params = new URLSearchParams({ warehouse_id: warehouseFilter, per_page: 200 });
    api.get(`/admin/bins?${params}`).then(async (res) => {
      if (!res?.ok) return;
      const json = await res.json();
      setBins(json.bins || []);
      // Reset bin filter if the previously-selected bin doesn't belong to
      // the new warehouse (prevents stale filter rejecting all results).
      if (binFilter) {
        const list = json.bins || [];
        const stillValid = list.some((b) => (b.bin_id || b.id) === binFilter);
        if (!stillValid) setBinFilter(null);
      }
    });
  }, [warehouseFilter]);

  // Run the inventory query whenever any filter changes.
  useEffect(() => {
    if (!warehouseFilter) return;
    const params = new URLSearchParams({ warehouse_id: warehouseFilter, page, per_page: 50 });
    if (binFilter) params.set('bin_id', binFilter);
    if (search) params.set('q', search);
    setLoading(true);
    api.get(`/admin/inventory?${params}`).then(async (res) => {
      setLoading(false);
      if (!res?.ok) return;
      const json = await res.json();
      setData(json.inventory || []);
      setPagination({ page: json.page, pages: json.pages, total: json.total, per_page: json.per_page });
    });
  }, [page, search, warehouseFilter, binFilter]);

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
      <div className="filter-bar" style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <select
          className="form-input"
          value={warehouseFilter || ''}
          onChange={(e) => { setWarehouseFilter(Number(e.target.value) || null); setPage(1); }}
          style={{ minWidth: 180 }}
        >
          <option value="">— Select warehouse —</option>
          {warehouses.map((w) => {
            const wId = w.warehouse_id || w.id;
            return (
              <option key={wId} value={wId}>
                {(w.warehouse_code || w.code)} — {(w.warehouse_name || w.name)}
              </option>
            );
          })}
        </select>
        <select
          className="form-input"
          value={binFilter || ''}
          onChange={(e) => { setBinFilter(Number(e.target.value) || null); setPage(1); }}
          style={{ minWidth: 200 }}
          disabled={!warehouseFilter || bins.length === 0}
        >
          <option value="">— All bins in warehouse —</option>
          {bins.map((b) => {
            const bId = b.bin_id || b.id;
            return (
              <option key={bId} value={bId}>
                {b.bin_code}{b.zone_name ? ` (${b.zone_name})` : ''}
              </option>
            );
          })}
        </select>
        <input
          className="form-input"
          placeholder="Search by SKU or item name (press Enter)"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') commitSearch(); }}
          onBlur={commitSearch}
          style={{ minWidth: 280, flex: 1 }}
        />
        {loading && <span style={{ color: 'rgba(255,255,255,0.5)', fontSize: 12 }}>Loading…</span>}
      </div>
      {!warehouseFilter && (
        <div style={{ padding: 24, color: 'rgba(255,255,255,0.5)', fontSize: 13 }}>
          Select a warehouse to view inventory.
        </div>
      )}
      {warehouseFilter && (
        <DataTable
          columns={columns}
          data={sorted}
          pagination={pagination}
          onPageChange={setPage}
          sortKey={sortKey}
          sortDir={sortDir}
          onSort={handleSort}
        />
      )}
    </div>
  );
}
