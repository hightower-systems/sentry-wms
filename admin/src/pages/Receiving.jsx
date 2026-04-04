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

  useEffect(() => {
    loadPOs();
  }, []);

  async function loadPOs() {
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
  }

  async function viewPO(po) {
    setSelected(po);
    const res = await api.get(`/admin/purchase-orders/${po.id}`);
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
    { key: 'quantity_expected', label: 'Expected' },
    { key: 'quantity_received', label: 'Received' },
    { key: 'remaining', label: 'Remaining', render: (r) => (r.quantity_expected || 0) - (r.quantity_received || 0) },
  ];

  return (
    <div>
      <PageHeader title="Receiving" />
      <DataTable columns={columns} data={pos} onRowClick={viewPO} emptyMessage="No open purchase orders" />

      {selected && detail && (
        <Modal title={`PO ${detail.po_number || selected.po_number}`} onClose={() => { setSelected(null); setDetail(null); }}>
          <div className="detail-grid">
            <span className="detail-label">Vendor</span>
            <span>{detail.vendor_name}</span>
            <span className="detail-label">Status</span>
            <span><StatusTag status={detail.status} /></span>
            <span className="detail-label">Expected</span>
            <span className="mono">{detail.expected_date || '-'}</span>
          </div>
          <div className="section-title">Lines</div>
          <DataTable columns={lineCols} data={detail.lines || []} />
        </Modal>
      )}
    </div>
  );
}
