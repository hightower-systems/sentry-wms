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
  const [selectedCount, setSelectedCount] = useState(null);

  useEffect(() => {
    loadCounts();
  }, []);

  async function loadCounts() {
    const res = await api.get('/admin/cycle-counts');
    if (res?.ok) {
      const data = await res.json();
      setCounts(data.counts || []);
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
    { key: 'count_id', label: 'ID', mono: true, render: (r) => `#${r.count_id}` },
    { key: 'bin_code', label: 'Bin', mono: true },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'assigned_to', label: 'Assigned To' },
    { key: 'lines', label: 'Items', render: (r) => r.lines?.length || 0 },
    { key: 'variance', label: 'Variances', render: (r) => {
      const variances = (r.lines || []).filter((l) => l.variance && l.variance !== 0);
      return variances.length > 0
        ? <span style={{ color: 'var(--copper)', fontWeight: 600 }}>{variances.length}</span>
        : <span style={{ color: 'var(--text-secondary)' }}>0</span>;
    }},
    { key: 'created_at', label: 'Created', mono: true, render: (r) => r.created_at ? new Date(r.created_at).toLocaleString() : '-' },
    { key: 'actions', label: '', render: (r) => (
      <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); setSelectedCount(r); }}>View</button>
    )},
  ];

  return (
    <div>
      <PageHeader title="Cycle Counts">
        <button className="btn btn-primary" onClick={openCreate}>New Count</button>
      </PageHeader>

      {message && (
        <div style={{ marginBottom: 12, fontSize: 13, color: 'var(--success)' }}>{message}</div>
      )}

      <DataTable columns={columns} data={counts} emptyMessage="No cycle counts found" />

      {/* Create modal */}
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

      {/* Detail modal */}
      {selectedCount && (
        <Modal
          title={`Count #${selectedCount.count_id} - ${selectedCount.bin_code}`}
          onClose={() => setSelectedCount(null)}
          footer={<button className="btn" onClick={() => setSelectedCount(null)}>Close</button>}
        >
          <div style={{ marginBottom: 12 }}>
            <div className="detail-grid">
              <span className="detail-label">Status</span><span><StatusTag status={selectedCount.status} /></span>
              <span className="detail-label">Assigned To</span><span>{selectedCount.assigned_to || '-'}</span>
              <span className="detail-label">Created</span><span className="mono">{selectedCount.created_at ? new Date(selectedCount.created_at).toLocaleString() : '-'}</span>
              {selectedCount.completed_at && (
                <><span className="detail-label">Completed</span><span className="mono">{new Date(selectedCount.completed_at).toLocaleString()}</span></>
              )}
            </div>
          </div>

          {selectedCount.lines?.length > 0 ? (
            <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)' }}>
                  <th style={{ textAlign: 'left', padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 }}>SKU</th>
                  <th style={{ textAlign: 'left', padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 }}>Item</th>
                  <th style={{ textAlign: 'right', padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 }}>Expected</th>
                  <th style={{ textAlign: 'right', padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 }}>Counted</th>
                  <th style={{ textAlign: 'right', padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 }}>Variance</th>
                </tr>
              </thead>
              <tbody>
                {selectedCount.lines.map((l) => {
                  const hasVariance = l.variance && l.variance !== 0;
                  return (
                    <tr key={l.count_line_id} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td className="mono" style={{ padding: '6px 8px' }}>{l.sku}</td>
                      <td style={{ padding: '6px 8px', color: 'var(--text-secondary)' }}>{l.item_name}</td>
                      <td className="mono" style={{ padding: '6px 8px', textAlign: 'right' }}>{l.expected_quantity}</td>
                      <td className="mono" style={{ padding: '6px 8px', textAlign: 'right' }}>{l.counted_quantity ?? '-'}</td>
                      <td className="mono" style={{ padding: '6px 8px', textAlign: 'right', color: hasVariance ? 'var(--copper)' : 'var(--text-secondary)', fontWeight: hasVariance ? 600 : 400 }}>
                        {l.variance != null ? (l.variance > 0 ? `+${l.variance}` : l.variance) : '-'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>No line items</p>
          )}
        </Modal>
      )}
    </div>
  );
}
