import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';
import StatusTag from '../components/StatusTag.jsx';
import Modal from '../components/Modal.jsx';

export default function Picking() {
  const { warehouseId } = useWarehouse();
  const [orders, setOrders] = useState([]);
  const [detail, setDetail] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({});
  const [orderLines, setOrderLines] = useState([{ item_id: '', quantity_ordered: 1 }]);
  const [items, setItems] = useState([]);
  const [warehouses, setWarehouses] = useState([]);
  const [error, setError] = useState('');

  useEffect(() => { if (warehouseId) loadOrders(); }, [warehouseId]);

  async function loadOrders() {
    const responses = await Promise.all([
      api.get(`/admin/sales-orders?status=OPEN&warehouse_id=${warehouseId}&per_page=50`),
      api.get(`/admin/sales-orders?status=ALLOCATED&warehouse_id=${warehouseId}&per_page=50`),
    ]);
    const all = [];
    for (const res of responses) {
      if (res?.ok) {
        const data = await res.json();
        all.push(...(data.sales_orders || []));
      }
    }
    setOrders(all);
  }

  async function openCreate() {
    setForm({ so_number: '', customer_name: '', customer_phone: '', customer_address: '', ship_method: '', ship_address: '', ship_by_date: '', warehouse_id: '' });
    setOrderLines([{ item_id: '', quantity_ordered: 1 }]);
    setError('');
    // Load items and warehouses for dropdowns
    const [itemRes, whRes] = await Promise.all([
      api.get('/admin/items?per_page=500&active=true'),
      api.get('/admin/warehouses'),
    ]);
    if (itemRes?.ok) {
      const data = await itemRes.json();
      setItems(data.items || []);
    }
    if (whRes?.ok) {
      const data = await whRes.json();
      setWarehouses(data.warehouses || []);
    }
    setShowCreate(true);
  }

  function addLine() {
    setOrderLines([...orderLines, { item_id: '', quantity_ordered: 1 }]);
  }

  function removeLine(index) {
    setOrderLines(orderLines.filter((_, i) => i !== index));
  }

  function updateLine(index, field, value) {
    setOrderLines(orderLines.map((l, i) => i === index ? { ...l, [field]: value } : l));
  }

  async function createSO() {
    setError('');
    if (!form.so_number) { setError('SO Number is required'); return; }
    if (!form.warehouse_id) { setError('Warehouse is required'); return; }
    const validLines = orderLines.filter((l) => l.item_id && l.quantity_ordered > 0);
    if (validLines.length === 0) { setError('At least one order line is required'); return; }

    const body = {
      so_number: form.so_number,
      customer_name: form.customer_name,
      customer_phone: form.customer_phone,
      customer_address: form.customer_address,
      warehouse_id: Number(form.warehouse_id),
      ship_method: form.ship_method || null,
      ship_address: form.ship_address || null,
      ship_by_date: form.ship_by_date || null,
      lines: validLines.map((l, i) => ({
        item_id: Number(l.item_id),
        quantity_ordered: Number(l.quantity_ordered),
        line_number: i + 1,
      })),
    };

    const res = await api.post('/admin/sales-orders', body);
    if (res?.ok) {
      setShowCreate(false);
      loadOrders();
    } else {
      const data = await res?.json();
      setError(data?.error || 'Failed to create SO');
    }
  }

  const columns = [
    { key: 'so_number', label: 'SO Number', mono: true, render: (r) => r.so_number || r.order_number },
    { key: 'customer_name', label: 'Customer' },
    { key: 'lines', label: 'Lines', render: (r) => r.lines?.length ?? r.line_count ?? '-' },
    { key: 'total_qty', label: 'Qty', render: (r) => r.lines?.reduce((s, l) => s + l.quantity, 0) ?? '-' },
    { key: 'status', label: 'Status', render: (r) => <StatusTag status={r.status} /> },
    { key: 'ship_by_date', label: 'Ship by', mono: true, render: (r) => r.ship_by_date || r.ship_by || '-' },
  ];

  return (
    <div>
      <PageHeader title="Picking">
        <button className="btn btn-primary" onClick={openCreate}>New SO</button>
      </PageHeader>
      <div className="section">
        <div className="section-title">Orders ready to pick</div>
        <DataTable columns={columns} data={orders} emptyMessage="No orders ready for picking" onRowClick={(r) => setDetail(r)} />
      </div>

      {detail && (
        <Modal title={detail.so_number || detail.order_number} onClose={() => setDetail(null)}
          footer={<button className="btn" onClick={() => setDetail(null)}>Close</button>}
        >
          <div className="detail-grid">
            <span className="detail-label">Customer</span><span>{detail.customer_name || '-'}</span>
            <span className="detail-label">Phone</span><span>{detail.customer_phone || '-'}</span>
            <span className="detail-label">Address</span><span>{detail.customer_address || detail.ship_address || '-'}</span>
            <span className="detail-label">Status</span><span><StatusTag status={detail.status} /></span>
            <span className="detail-label">Ship By</span><span className="mono">{detail.ship_by_date || detail.ship_by || '-'}</span>
            <span className="detail-label">Priority</span><span>{detail.priority || '-'}</span>
          </div>
        </Modal>
      )}

      {showCreate && (
        <Modal title="New Sales Order" onClose={() => setShowCreate(false)}
          footer={
            <>
              <button className="btn" onClick={() => setShowCreate(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={createSO}>Create</button>
            </>
          }
        >
          {error && <div className="form-error" style={{ marginBottom: 12 }}>{error}</div>}
          <div className="form-row">
            <div className="form-group">
              <label>SO Number *</label>
              <input className="form-input" value={form.so_number || ''} onChange={(e) => setForm({ ...form, so_number: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Warehouse *</label>
              <select className="form-select" value={form.warehouse_id || ''} onChange={(e) => setForm({ ...form, warehouse_id: e.target.value })}>
                <option value="">Select...</option>
                {warehouses.map((w) => <option key={w.warehouse_id} value={w.warehouse_id}>{w.warehouse_code} - {w.warehouse_name}</option>)}
              </select>
            </div>
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>Customer Name</label>
              <input className="form-input" value={form.customer_name || ''} onChange={(e) => setForm({ ...form, customer_name: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Customer Phone</label>
              <input className="form-input" value={form.customer_phone || ''} onChange={(e) => setForm({ ...form, customer_phone: e.target.value })} />
            </div>
          </div>
          <div className="form-group">
            <label>Customer Address</label>
            <input className="form-input" value={form.customer_address || ''} onChange={(e) => setForm({ ...form, customer_address: e.target.value })} />
          </div>
          <div className="form-row">
            <div className="form-group">
              <label>Ship Method</label>
              <input className="form-input" value={form.ship_method || ''} onChange={(e) => setForm({ ...form, ship_method: e.target.value })} />
            </div>
            <div className="form-group">
              <label>Ship By Date</label>
              <input className="form-input" type="date" value={form.ship_by_date || ''} onChange={(e) => setForm({ ...form, ship_by_date: e.target.value })} />
            </div>
          </div>
          <div className="form-group">
            <label>Ship Address</label>
            <input className="form-input" value={form.ship_address || ''} onChange={(e) => setForm({ ...form, ship_address: e.target.value })} />
          </div>

          <div style={{ marginTop: 16, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <label style={{ fontWeight: 600 }}>Order Lines *</label>
              <button className="btn btn-sm" onClick={addLine}>+ Add Line</button>
            </div>
            {orderLines.map((line, i) => (
              <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
                <select
                  className="form-select"
                  style={{ flex: 2 }}
                  value={line.item_id}
                  onChange={(e) => updateLine(i, 'item_id', e.target.value)}
                >
                  <option value="">Select item...</option>
                  {items.map((it) => <option key={it.id || it.item_id} value={it.id || it.item_id}>{it.sku} - {it.item_name}</option>)}
                </select>
                <input
                  className="form-input"
                  type="number"
                  min="1"
                  style={{ flex: 0, width: 80 }}
                  value={line.quantity_ordered}
                  onChange={(e) => updateLine(i, 'quantity_ordered', e.target.value)}
                  placeholder="Qty"
                />
                {orderLines.length > 1 && (
                  <button className="btn btn-sm btn-danger" onClick={() => removeLine(i)} style={{ padding: '4px 8px' }}>X</button>
                )}
              </div>
            ))}
          </div>
        </Modal>
      )}
    </div>
  );
}
