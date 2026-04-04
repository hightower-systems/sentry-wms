import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

export default function CycleCounts() {
  const [counts, setCounts] = useState([]);
  const [showCreate, setShowCreate] = useState(false);
  const [bins, setBins] = useState([]);
  const [selectedBins, setSelectedBins] = useState([]);
  const [message, setMessage] = useState('');

  useEffect(() => {
    loadCounts();
  }, []);

  async function loadCounts() {
    const res = await api.get('/admin/audit-log?action_type=CYCLE_COUNT_CREATED&per_page=50');
    if (res?.ok) {
      const data = await res.json();
      setCounts(data.audit_log || []);
    }
  }

  async function openCreate() {
    const res = await api.get('/admin/bins?warehouse_id=1');
    if (res?.ok) {
      const data = await res.json();
      setBins(data.bins || []);
    }
    setSelectedBins([]);
    setShowCreate(true);
  }

  function toggleBin(id) {
    setSelectedBins((prev) =>
      prev.includes(id) ? prev.filter((b) => b !== id) : [...prev, id]
    );
  }

  async function createCount() {
    if (selectedBins.length === 0) return;
    const res = await api.post('/inventory/cycle-count/create', {
      bin_ids: selectedBins,
      warehouse_id: 1,
    });
    if (res?.ok) {
      setMessage('Cycle count created');
      setShowCreate(false);
      loadCounts();
    } else {
      const data = await res?.json();
      setMessage(data?.error || 'Failed to create count');
    }
  }

  const columns = [
    { key: 'created_at', label: 'Created', mono: true, render: (r) => new Date(r.created_at).toLocaleString() },
    { key: 'action_type', label: 'Action' },
    { key: 'entity_type', label: 'Entity' },
    { key: 'username', label: 'User' },
    { key: 'details', label: 'Details', render: (r) => {
      if (!r.details) return '-';
      try {
        const d = typeof r.details === 'string' ? JSON.parse(r.details) : r.details;
        return d.count_id ? `Count #${d.count_id}` : JSON.stringify(d).slice(0, 60);
      } catch { return '-'; }
    }},
  ];

  return (
    <div>
      <PageHeader title="Cycle Counts">
        <button className="btn btn-primary" onClick={openCreate}>New Count</button>
      </PageHeader>

      {message && (
        <div style={{ marginBottom: 12, fontSize: 13, color: 'var(--success)' }}>{message}</div>
      )}

      <DataTable columns={columns} data={counts} emptyMessage="No cycle counts recorded" />

      {showCreate && (
        <Modal
          title="Create Cycle Count"
          onClose={() => setShowCreate(false)}
          footer={
            <>
              <button className="btn" onClick={() => setShowCreate(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={createCount} disabled={selectedBins.length === 0}>
                Create Count ({selectedBins.length} bins)
              </button>
            </>
          }
        >
          <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
            Select bins to count:
          </p>
          <div style={{ maxHeight: 300, overflow: 'auto' }}>
            {bins.map((bin) => (
              <label key={bin.id} style={{ display: 'flex', gap: 8, padding: '4px 0', fontSize: 13, cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={selectedBins.includes(bin.id)}
                  onChange={() => toggleBin(bin.id)}
                />
                <span className="mono">{bin.bin_code}</span>
                <span style={{ color: 'var(--text-secondary)' }}>{bin.zone_name || ''}</span>
              </label>
            ))}
          </div>
        </Modal>
      )}
    </div>
  );
}
