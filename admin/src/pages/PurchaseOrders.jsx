import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import Modal from '../components/Modal.jsx';
import StatusTag from '../components/StatusTag.jsx';

const STATUS_OPTIONS = ['All', 'OPEN', 'PARTIAL', 'RECEIVED', 'CLOSED'];

export default function PurchaseOrders() {
  const [orders, setOrders] = useState([]);
  const [pagination, setPagination] = useState(null);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('All');
  const [selectedPO, setSelectedPO] = useState(null);
  const [poLines, setPOLines] = useState([]);

  useEffect(() => { loadOrders(); }, [page, statusFilter]);

  async function loadOrders() {
    const params = `?page=${page}&per_page=50${statusFilter !== 'All' ? `&status=${statusFilter}` : ''}`;
    const res = await api.get(`/admin/purchase-orders${params}`);
    if (res?.ok) {
      const data = await res.json();
      setOrders(data.purchase_orders || []);
      setPagination({ page: data.page, pages: data.pages, total: data.total });
    }
  }

  async function viewPO(po) {
    const res = await api.get(`/admin/purchase-orders/${po.po_id || po.id}`);
    if (res?.ok) {
      const data = await res.json();
      setSelectedPO(data.purchase_order);
      setPOLines(data.lines || []);
    }
  }

  function handleStatusChange(e) {
    setStatusFilter(e.target.value);
    setPage(1);
  }

  function handlePageChange(newPage) {
    setPage(newPage);
  }

  const columns = [
    { key: 'po_number', label: 'PO Number', mono: true },
    { key: 'vendor', label: 'Vendor' },
    { key: 'line_count', label: 'Lines' },
    { key: 'expected_date', label: 'Expected Date', mono: true, render: (r) => r.expected_date ? new Date(r.expected_date).toLocaleDateString() : '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'created_at', label: 'Created', render: (r) => r.created_at ? new Date(r.created_at).toLocaleDateString() : '-' },
  ];

  const thStyle = { textAlign: 'left', padding: '6px 8px', fontSize: 11, color: 'var(--text-secondary)', fontWeight: 600 };
  const tdStyle = { padding: '6px 8px' };

  return (
    <div>
      <PageHeader title="Purchase Orders" />

      <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
        <label style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Status:</label>
        <select className="form-select" value={statusFilter} onChange={handleStatusChange} style={{ width: 160 }}>
          {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      <DataTable
        columns={columns}
        data={orders}
        pagination={pagination}
        onPageChange={handlePageChange}
        onRowClick={viewPO}
        emptyMessage="No purchase orders found"
      />

      {selectedPO && (
        <Modal
          title={`PO ${selectedPO.po_number}`}
          onClose={() => { setSelectedPO(null); setPOLines([]); }}
          footer={<button className="btn" onClick={() => { setSelectedPO(null); setPOLines([]); }}>Close</button>}
        >
          <div className="detail-grid" style={{ marginBottom: 16 }}>
            <span className="detail-label">Vendor</span><span>{selectedPO.vendor || '-'}</span>
            <span className="detail-label">Status</span><span><StatusTag status={selectedPO.status} /></span>
            <span className="detail-label">Expected Date</span><span className="mono">{selectedPO.expected_date ? new Date(selectedPO.expected_date).toLocaleDateString() : '-'}</span>
          </div>

          {poLines.length > 0 ? (
            <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)' }}>
                  <th style={thStyle}>SKU</th>
                  <th style={thStyle}>Item Name</th>
                  <th style={{ ...thStyle, textAlign: 'right' }}>Ordered</th>
                  <th style={{ ...thStyle, textAlign: 'right' }}>Received</th>
                  <th style={{ ...thStyle, textAlign: 'right' }}>Remaining</th>
                </tr>
              </thead>
              <tbody>
                {poLines.map((l, i) => {
                  const remaining = (l.quantity_ordered || 0) - (l.quantity_received || 0);
                  return (
                    <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                      <td className="mono" style={tdStyle}>{l.sku}</td>
                      <td style={{ ...tdStyle, color: 'var(--text-secondary)' }}>{l.item_name}</td>
                      <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.quantity_ordered}</td>
                      <td className="mono" style={{ ...tdStyle, textAlign: 'right' }}>{l.quantity_received}</td>
                      <td className="mono" style={{ ...tdStyle, textAlign: 'right', color: remaining > 0 ? 'var(--copper)' : 'var(--text-secondary)', fontWeight: remaining > 0 ? 600 : 400 }}>{remaining}</td>
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
