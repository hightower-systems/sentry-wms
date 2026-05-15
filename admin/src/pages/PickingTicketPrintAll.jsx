import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import { TicketDocument } from './PickingTicketPrint.jsx';
import './pickingTicket.css';

const PICKABLE_STATUSES = ['OPEN', 'ALLOCATED', 'PICKING', 'PICKED'];

export default function PickingTicketPrintAll() {
  const [params] = useSearchParams();
  const status = params.get('status') || 'OPEN';
  const warehouseId = params.get('warehouse_id') || '';
  const [tickets, setTickets] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const printedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError('');
      const statuses = status === 'ALL' ? PICKABLE_STATUSES : [status];
      const listResponses = await Promise.all(
        statuses.map((s) => {
          const qs = new URLSearchParams({ status: s, per_page: '50' });
          if (warehouseId) qs.set('warehouse_id', warehouseId);
          return api.get(`/admin/sales-orders?${qs.toString()}`);
        }),
      );
      const orders = [];
      for (const res of listResponses) {
        if (res?.ok) {
          const data = await res.json();
          orders.push(...(data.sales_orders || []));
        }
      }
      if (cancelled) return;
      if (orders.length === 0) {
        setTickets([]);
        setLoading(false);
        return;
      }
      const detailResponses = await Promise.all(
        orders.map((o) => api.get(`/admin/sales-orders/${o.so_id}/picking-ticket`)),
      );
      const out = [];
      for (let i = 0; i < detailResponses.length; i++) {
        const res = detailResponses[i];
        if (!res?.ok) continue;
        const data = await res.json();
        if (data.sales_order) {
          out.push({ so: data.sales_order, lines: data.lines || [] });
        }
      }
      if (cancelled) return;
      if (out.length === 0) {
        setError('No tickets could be loaded.');
      }
      setTickets(out);
      setLoading(false);
    }
    load();
    return () => { cancelled = true; };
  }, [status, warehouseId]);

  // Auto-trigger the print dialog once tickets are rendered. Guarded
  // by a ref so React's StrictMode double-invoke doesn't fire it twice.
  useEffect(() => {
    if (loading || error || tickets.length === 0) return;
    if (printedRef.current) return;
    printedRef.current = true;
    // Defer to next tick so the DOM has flushed all ticket pages
    // before the browser snapshots them for print preview.
    const t = setTimeout(() => window.print(), 100);
    return () => clearTimeout(t);
  }, [loading, error, tickets.length]);

  return (
    <div className="pt-root">
      <div className="pt-toolbar pt-no-print">
        <Link to="/picking-tickets" className="pt-back">&larr; Back</Link>
        <span style={{ fontSize: '10pt', color: '#333' }}>
          {loading
            ? 'Loading tickets…'
            : `${tickets.length} ticket${tickets.length === 1 ? '' : 's'} (${status})`}
        </span>
        <button onClick={() => window.print()} disabled={loading || tickets.length === 0}>
          Print
        </button>
      </div>

      {loading && <div className="pt-page">Loading tickets…</div>}

      {!loading && error && (
        <div className="pt-page">
          <h2>Could not render tickets</h2>
          <p>{error}</p>
        </div>
      )}

      {!loading && !error && tickets.length === 0 && (
        <div className="pt-page">
          <h2>No tickets to print</h2>
          <p>No sales orders matched status {status}.</p>
        </div>
      )}

      {!loading && tickets.map(({ so, lines }) => (
        <TicketDocument key={so.so_id} so={so} lines={lines} />
      ))}
    </div>
  );
}
