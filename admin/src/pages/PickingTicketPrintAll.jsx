import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api.js';
import { TicketDocument } from './PickingTicketPrint.jsx';
import './pickingTicket.css';

const PICKABLE_STATUSES = ['OPEN', 'ALLOCATED', 'PICKING', 'PICKED'];

// Standalone print-queue view. Opened in a new tab from the picking
// tickets page; renders every ticket in the current status filter
// stacked one per page so the user can hit Ctrl/Cmd+P natively. No
// toolbar, no auto-print, no admin Layout chrome.
export default function PickingTicketPrintAll() {
  const [params] = useSearchParams();
  const status = params.get('status') || 'OPEN';
  const warehouseId = params.get('warehouse_id') || '';
  const [tickets, setTickets] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

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
      // Sort by ship_by_date ascending so the printer stack comes out
      // oldest-first, matching the default sort on the list page.
      // Nulls sink to the bottom so SOs without a ship-by date do not
      // hijack the top of the stack.
      orders.sort((a, b) => {
        const ad = a.ship_by_date;
        const bd = b.ship_by_date;
        if (!ad && !bd) return 0;
        if (!ad) return 1;
        if (!bd) return -1;
        return new Date(ad) - new Date(bd);
      });
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
      if (out.length === 0) setError('No tickets could be loaded.');
      setTickets(out);
      setLoading(false);
    }
    load();
    return () => { cancelled = true; };
  }, [status, warehouseId]);

  // Update the tab title once we know the count, so the user can
  // tell the queue tabs apart.
  useEffect(() => {
    if (loading) {
      document.title = 'Loading picking tickets…';
    } else if (error) {
      document.title = 'Picking tickets — error';
    } else {
      document.title = `Picking tickets (${tickets.length}) — ${status}`;
    }
  }, [loading, error, tickets.length, status]);

  if (loading) {
    return <div className="pt-root"><div className="pt-page">Loading tickets…</div></div>;
  }

  if (error) {
    return (
      <div className="pt-root">
        <div className="pt-page">
          <h2>Could not render tickets</h2>
          <p>{error}</p>
        </div>
      </div>
    );
  }

  if (tickets.length === 0) {
    return (
      <div className="pt-root">
        <div className="pt-page">
          <h2>No tickets to print</h2>
          <p>No sales orders matched status {status}.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="pt-root">
      {tickets.map(({ so, lines }) => (
        <TicketDocument key={so.so_id} so={so} lines={lines} />
      ))}
    </div>
  );
}
