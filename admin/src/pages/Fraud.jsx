import { useState, useEffect, useCallback } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';
import DataTable from '../components/DataTable.jsx';
import PageHeader from '../components/PageHeader.jsx';

// Renders one address as a 2-line summary -- street on line 1, city
// state postal on line 2. Used inline in the fraud row so the CSR
// can eyeball the mismatch without opening anything.
function AddressSummary({ line1, line2, city, state, postalCode }) {
  const street = [line1, line2].filter(Boolean).join(', ');
  const cityLine = [city, state, postalCode].filter(Boolean).join(' ');
  if (!street && !cityLine) return <span style={{ color: '#999' }}>(blank)</span>;
  return (
    <div style={{ fontSize: 12, lineHeight: 1.35 }}>
      {street && <div>{street}</div>}
      {cityLine && <div>{cityLine}</div>}
    </div>
  );
}

function MemoCell({ soId, initial, onSaved }) {
  const [value, setValue] = useState(initial || '');
  const [savedValue, setSavedValue] = useState(initial || '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function commit() {
    if (value === savedValue) return;
    setSaving(true);
    setError('');
    const res = await api.patch(`/admin/sales-orders/${soId}/memo`, { memo: value });
    setSaving(false);
    if (!res?.ok) {
      setError('Save failed');
      return;
    }
    setSavedValue(value);
    onSaved?.(value);
  }

  return (
    <div onClick={(e) => e.stopPropagation()}>
      <textarea
        className="form-input"
        rows={2}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={commit}
        placeholder="CSR notes…"
        style={{ width: '100%', minWidth: 240, fontSize: 12, resize: 'vertical' }}
      />
      {saving && <div style={{ fontSize: 11, color: '#666' }}>Saving…</div>}
      {error && <div className="form-error" style={{ fontSize: 11 }}>{error}</div>}
    </div>
  );
}

export default function Fraud() {
  const { warehouseId } = useWarehouse();
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [actionError, setActionError] = useState('');

  const load = useCallback(async () => {
    if (!warehouseId) return;
    setLoading(true);
    const qs = new URLSearchParams({
      status: 'FRAUD_REVIEW',
      warehouse_id: String(warehouseId),
      per_page: '100',
    });
    const res = await api.get(`/admin/sales-orders?${qs.toString()}`);
    if (res?.ok) {
      const data = await res.json();
      setOrders(data.sales_orders || []);
    }
    setLoading(false);
  }, [warehouseId]);

  useEffect(() => { load(); }, [load]);

  async function pushToQueue(soId) {
    setActionError('');
    const res = await api.post(`/admin/sales-orders/${soId}/push-to-queue`);
    if (!res?.ok) {
      setActionError(`Could not push order ${soId} to queue.`);
      return;
    }
    setOrders((prev) => prev.filter((o) => o.so_id !== soId));
  }

  const columns = [
    { key: 'so_number', label: 'SO Number', mono: true },
    { key: 'customer_name', label: 'Customer' },
    {
      key: 'billing',
      label: 'Billing',
      render: (r) => (
        <AddressSummary
          line1={r.billing_address_line1}
          line2={r.billing_address_line2}
          city={r.billing_address_city}
          state={r.billing_address_state}
          postalCode={r.billing_address_postal_code}
        />
      ),
    },
    {
      key: 'shipping',
      label: 'Shipping',
      render: (r) => (
        <AddressSummary
          line1={r.shipping_address_line1}
          line2={r.shipping_address_line2}
          city={r.shipping_address_city}
          state={r.shipping_address_state}
          postalCode={r.shipping_address_postal_code}
        />
      ),
    },
    {
      key: 'memo',
      label: 'Memo',
      render: (r) => (
        <MemoCell
          soId={r.so_id}
          initial={r.memo}
          onSaved={(v) => setOrders((prev) => prev.map(
            (o) => (o.so_id === r.so_id ? { ...o, memo: v } : o),
          ))}
        />
      ),
    },
    {
      key: 'actions',
      label: '',
      render: (r) => (
        <button
          className="btn btn-sm btn-primary"
          onClick={(e) => {
            e.stopPropagation();
            pushToQueue(r.so_id);
          }}
        >Push to queue</button>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title="Fraud Review" />
      {actionError && (
        <div className="section">
          <div className="form-error">{actionError}</div>
        </div>
      )}
      <div className="section">
        <div className="section-title">
          Orders flagged at ingest{!loading && ` (${orders.length})`}
        </div>
        <DataTable
          columns={columns}
          data={orders}
          emptyMessage={loading ? 'Loading…' : 'No flagged orders'}
        />
      </div>
    </div>
  );
}
