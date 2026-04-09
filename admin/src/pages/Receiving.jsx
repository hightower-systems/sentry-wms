import { useState, useEffect } from 'react';
import { api } from '../api.js';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';
import Modal from '../components/Modal.jsx';

export default function Receiving() {
  const [pos, setPos] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [statusFilter, setStatusFilter] = useState('active');

  useEffect(() => {
    loadPOs();
  }, [statusFilter]);

  async function loadPOs() {
    if (statusFilter === 'active') {
      const [openRes, partialRes] = await Promise.all([
        api.get('/admin/purchase-orders?status=OPEN&per_page=50'),
        api.get('/admin/purchase-orders?status=PARTIAL&per_page=50'),
      ]);
      const all = [];
      for (const res of [openRes, partialRes]) {
        if (res?.ok) {
          const data = await res.json();
          all.push(...(data.purchase_orders || []));
        }
      }
      setPos(all);
    } else {
      const params = new URLSearchParams({ per_page: 50 });
      if (statusFilter !== 'all') params.set('status', statusFilter);
      const res = await api.get(`/admin/purchase-orders?${params}`);
      if (res?.ok) {
        const data = await res.json();
        setPos(data.purchase_orders || []);
      }
    }
  }

  async function viewPO(po) {
    setSelected(po);
    const id = po.po_id || po.id;
    const res = await api.get(`/admin/purchase-orders/${id}`);
    if (res?.ok) {
      setDetail(await res.json());
    }
  }

  const columns = [
    { key: 'po_number', label: 'PO Number', mono: true },
    { key: 'vendor_name', label: 'Vendor' },
    { key: 'lines', label: 'Lines', render: (r) => r.lines?.length ?? '-' },
    { key: 'expected_date', label: 'Expected Date', mono: true, render: (r) => r.expected_date || '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
  ];

  const lineCols = [
    { key: 'sku', label: 'SKU', mono: true },
    { key: 'item_name', label: 'Item' },
    { key: 'quantity_ordered', label: 'Ordered' },
    { key: 'quantity_received', label: 'Received' },
    { key: 'remaining', label: 'Remaining', render: (r) => (r.quantity_ordered || 0) - (r.quantity_received || 0) },
  ];

  const po = detail?.purchase_order || detail;

  return (
    <div>
      <PageHeader title="Receiving" />
      <div className="filter-bar">
        <select className="form-select" style={{ width: 140 }} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="active">Open / Partial</option>
          <option value="all">All</option>
          <option value="OPEN">Open</option>
          <option value="PARTIAL">Partial</option>
          <option value="RECEIVED">Received</option>
          <option value="CLOSED">Closed</option>
        </select>
      </div>
      <DataTable columns={columns} data={pos} onRowClick={viewPO} emptyMessage="No purchase orders" />

      {selected && detail && (
        <Modal title={`PO ${po?.po_number || selected.po_number}`} onClose={() => { setSelected(null); setDetail(null); }}>
          <div className="detail-grid">
            <span className="detail-label">Vendor</span>
            <span>{po?.vendor_name}</span>
            <span className="detail-label">Status</span>
            <span><StatusTag status={po?.status} /></span>
            <span className="detail-label">Expected</span>
            <span className="mono">{po?.expected_date || '-'}</span>
          </div>
          <div className="section-title">Lines</div>
          <DataTable columns={lineCols} data={detail.lines || []} />
        </Modal>
      )}
    </div>
  );
}
