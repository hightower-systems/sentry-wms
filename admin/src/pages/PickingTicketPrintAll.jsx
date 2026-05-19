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
  // Optional comma-separated SO order from the list page. When
  // present, we reorder fetched SOs to match this sequence so the
  // print stack reflects the operator's chosen column sort. Absent
  // (e.g. someone deep-links this URL) we fall back to the legacy
  // primary_bin_pick_sequence sort.
  const requestedOrderParam = params.get('so_ids') || '';
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
          const qs = new URLSearchParams({
            status: s,
            per_page: '100',
            // Pull primary_bin_pick_sequence so we can sort the print
            // stack in warehouse-walk order. The picker fills the cart
            // in this order; the shipper unpacks in the same order.
            include_primary_bin: 'true',
          });
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
      const requestedIds = requestedOrderParam
        ? requestedOrderParam.split(',').map((s) => parseInt(s, 10)).filter(Number.isFinite)
        : [];
      if (requestedIds.length > 0) {
        // Honor the list page's sort. Build a rank map so SOs the
        // operator did not see (status changed between tabs, late
        // arrivals after they hit Print All) sink to the bottom but
        // still print rather than getting silently dropped.
        const rank = new Map(requestedIds.map((id, i) => [id, i]));
        orders.sort((a, b) => {
          const ra = rank.has(a.so_id) ? rank.get(a.so_id) : Number.MAX_SAFE_INTEGER;
          const rb = rank.has(b.so_id) ? rank.get(b.so_id) : Number.MAX_SAFE_INTEGER;
          return ra - rb;
        });
      } else {
        // Legacy fallback for deep-linked print URLs without an
        // explicit order. Sort by primary_bin_pick_sequence ascending
        // so the printer stack matches the picker's physical walk
        // through the warehouse. SOs without a primary bin (no
        // preferred_bins for their items) sink to the bottom with a
        // ship_by_date tiebreak so they remain ordered among
        // themselves.
        orders.sort((a, b) => {
          const aps = a.primary_bin_pick_sequence;
          const bps = b.primary_bin_pick_sequence;
          if (aps != null && bps != null) return aps - bps;
          if (aps != null) return -1;
          if (bps != null) return 1;
          const ad = a.ship_by_date;
          const bd = b.ship_by_date;
          if (!ad && !bd) return 0;
          if (!ad) return 1;
          if (!bd) return -1;
          return new Date(ad) - new Date(bd);
        });
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
      if (out.length === 0) setError('No tickets could be loaded.');
      setTickets(out);
      setLoading(false);
      // mig 057: only mark SOs whose ticket data actually made it into
      // the render array. Orders that failed earlier (404, network
      // error, malformed payload) stay unprinted so they reappear in
      // the queue and the operator can retry rather than silently
      // losing them.
      if (out.length > 0) {
        api.post('/admin/sales-orders/mark-printed', {
          so_ids: out.map((t) => t.so.so_id),
        }).catch(() => {});
      }
    }
    load();
    return () => { cancelled = true; };
  }, [status, warehouseId, requestedOrderParam]);

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
