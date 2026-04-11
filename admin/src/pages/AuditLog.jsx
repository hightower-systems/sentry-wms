import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';

export default function AuditLog() {
  const [logs, setLogs] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState({ action_type: '', user_id: '', start_date: '', end_date: '' });
  const [selected, setSelected] = useState(null);

  useEffect(() => { loadLogs(); }, [page, filters]);

  async function loadLogs() {
    const params = new URLSearchParams({ page, per_page: 50 });
    if (filters.action_type) params.set('action_type', filters.action_type);
    if (filters.user_id) params.set('user_id', filters.user_id);
    if (filters.start_date) params.set('start_date', filters.start_date);
    if (filters.end_date) params.set('end_date', filters.end_date);
    const res = await api.get(`/admin/audit-log?${params}`);
    if (res?.ok) {
      const data = await res.json();
      setLogs(data.entries || []);
      setPagination({ page: data.page, pages: data.pages, total: data.total, per_page: data.per_page });
    }
  }

  function updateFilter(key, value) {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setPage(1);
  }

  function formatDetails(row) {
    if (!row.details) return '-';
    try {
      const d = typeof row.details === 'string' ? JSON.parse(row.details) : row.details;
      const parts = [];
      for (const [k, v] of Object.entries(d)) {
        if (parts.length >= 3) { parts.push('...'); break; }
        parts.push(`${k}: ${v}`);
      }
      return parts.join(', ');
    } catch {
      return String(row.details).slice(0, 60);
    }
  }

  const columns = [
    { key: 'created_at', label: 'Timestamp', mono: true, render: (r) => new Date(r.created_at).toLocaleString() },
    { key: 'action_type', label: 'Action' },
    { key: 'entity_type', label: 'Entity', render: (r) => r.entity_name ? `${r.entity_type}: ${r.entity_name}` : r.entity_type },
    { key: 'username', label: 'User' },
    { key: 'details', label: 'Details', render: formatDetails },
  ];

  return (
    <div>
      <PageHeader title="Audit Log" />
      <div className="filter-bar">
        <input className="form-input" placeholder="Action type..." value={filters.action_type} onChange={(e) => updateFilter('action_type', e.target.value)} />
        <input className="form-input" placeholder="User ID..." value={filters.user_id} onChange={(e) => updateFilter('user_id', e.target.value)} style={{ width: 100 }} />
        <input className="form-input" type="date" value={filters.start_date} onChange={(e) => updateFilter('start_date', e.target.value)} />
        <input className="form-input" type="date" value={filters.end_date} onChange={(e) => updateFilter('end_date', e.target.value)} />
      </div>
      <DataTable columns={columns} data={logs} pagination={pagination} onPageChange={setPage} emptyMessage="No audit log entries" onRowClick={setSelected} />

      {selected && (
        <Modal title="Audit Log Detail" onClose={() => setSelected(null)}>
          <div className="detail-grid">
            <span className="detail-label">Timestamp</span><span className="mono">{new Date(selected.created_at).toLocaleString()}</span>
            <span className="detail-label">Action</span><span>{selected.action_type}</span>
            <span className="detail-label">Entity</span><span>{selected.entity_type}{selected.entity_name ? `: ${selected.entity_name}` : ''} (ID: {selected.entity_id})</span>
            <span className="detail-label">User</span><span>{selected.username}</span>
            {selected.device_id && <><span className="detail-label">Device</span><span>{selected.device_id}</span></>}
            {selected.warehouse_code && <><span className="detail-label">Warehouse</span><span className="mono">{selected.warehouse_code}</span></>}
          </div>
          {selected.details && (
            <div style={{ marginTop: 16 }}>
              <h4 style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.5px' }}>Event Details</h4>
              <div className="detail-grid">
                {Object.entries(typeof selected.details === 'string' ? JSON.parse(selected.details) : selected.details).map(([k, v]) => (
                  <><span key={`${k}-l`} className="detail-label">{k}</span><span key={`${k}-v`} className="mono">{String(v)}</span></>
                ))}
              </div>
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}
