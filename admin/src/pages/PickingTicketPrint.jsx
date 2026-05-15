import { useEffect, useMemo, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { api } from '../api.js';
import { encodeCode128B } from '../utils/code128.js';
import './pickingTicket.css';

function formatUSDate(value) {
  if (!value) return '';
  // Accept both 'YYYY-MM-DD' and ISO timestamps. Building a Date from
  // 'YYYY-MM-DD' alone hits the UTC-midnight gotcha (shifts a day in
  // negative TZ), so split and rebuild as local.
  const datePart = String(value).slice(0, 10);
  const [y, m, d] = datePart.split('-').map(Number);
  if (!y || !m || !d) return '';
  return `${String(m).padStart(2, '0')}/${String(d).padStart(2, '0')}/${y}`;
}

function shippingAddressLines(so) {
  const lines = [];
  const name = so.shipping_address_name || so.customer_name || '';
  if (name) lines.push(name);
  if (so.shipping_address_line1) lines.push(so.shipping_address_line1);
  if (so.shipping_address_line2) lines.push(so.shipping_address_line2);
  const cityStateZip = [
    so.shipping_address_city,
    so.shipping_address_state,
    so.shipping_address_postal_code,
  ].filter(Boolean).join(' ');
  if (cityStateZip) lines.push(cityStateZip);
  if (lines.length === 0 && so.ship_address) {
    // Legacy single-string ship address fallback. Split on newlines so
    // multi-line legacy data still stacks visually.
    return String(so.ship_address).split(/\r?\n/).filter(Boolean);
  }
  return lines;
}

function BarcodeSvg({ value, className, modulePx, height }) {
  // Render Code 128 modules as <rect> elements. We never inject the
  // user-supplied value into SVG text content, only into the encoder
  // (which only emits numeric module widths) -- so this stays safe
  // against arbitrary input.
  const { bars, width } = useMemo(() => {
    const { segments, totalModules } = encodeCode128B(value || '');
    const out = [];
    let cursor = 0;
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i];
      const segWidth = seg.width * modulePx;
      if (seg.black) out.push({ key: i, x: cursor, w: segWidth });
      cursor += segWidth;
    }
    return { bars: out, width: totalModules * modulePx };
  }, [value, modulePx]);
  if (width === 0) return null;
  return (
    <svg
      className={className}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Barcode ${value}`}
    >
      {bars.map((b) => (
        <rect key={b.key} x={b.x} y={0} width={b.w} height={height} fill="#000" />
      ))}
    </svg>
  );
}

export function TicketDocument({ so, lines }) {
  const orderNumber = so.so_number || '';
  const addressLines = shippingAddressLines(so);
  return (
    <div className="pt-page">
      <table className="pt-header">
        <tbody>
          <tr>
            <td rowSpan={3} className="pt-logo-cell">
              <div className="pt-logo-row">
                <img src="/picking-tickets/avidmax_logo.png" className="pt-logo" alt="AvidMax" />
                <div className="pt-nameandaddress">
                  7399 S Tucson Way<br />
                  Ste A3<br />
                  Centennial CO, 80112<br />
                  866-454-5523
                </div>
              </div>
            </td>
            <td className="pt-right"><span className="pt-title">Packing Slip</span></td>
          </tr>
          <tr>
            <td className="pt-right"><span className="pt-number">Order #{orderNumber}</span></td>
          </tr>
          <tr>
            <td className="pt-right">Must ship by: {formatUSDate(so.ship_by_date)}</td>
          </tr>
        </tbody>
      </table>

      <br />

      <table>
        <tbody>
          <tr>
            <td className="pt-address-header"><b>Shipping Address</b></td>
            <td></td>
          </tr>
          <tr>
            <td className="pt-address">
              <b>Ship To:</b><br />
              {addressLines.map((l, i) => (
                <span key={i}>{l}{i < addressLines.length - 1 ? <br /> : null}</span>
              ))}
            </td>
            <td className="pt-right">
              <BarcodeSvg
                value={orderNumber}
                className="pt-barcode-small"
                modulePx={1}
                height={32}
              />
            </td>
          </tr>
        </tbody>
      </table>

      <table className="pt-body">
        <tbody>
          <tr><th>Shipping Method</th><th>Order Date</th></tr>
          <tr>
            <td>{so.ship_method || ''}</td>
            <td>{formatUSDate(so.order_date || so.created_at)}</td>
          </tr>
        </tbody>
      </table>

      <table className="pt-itemtable">
        <thead>
          <tr>
            <th className="pt-center" colSpan={3}>Qty</th>
            <th className="pt-center" colSpan={3}>Shipped</th>
            <th colSpan={16}>Item</th>
            <th colSpan={3}>Box</th>
            <th className="pt-right" colSpan={5}>Bin</th>
            <th className="pt-right" colSpan={4}>UPC</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((l) => (
            <tr key={l.so_line_id}>
              <td className="pt-center" colSpan={3} style={{ lineHeight: '150%' }}>{l.quantity_ordered}</td>
              <td className="pt-center" colSpan={3} style={{ lineHeight: '150%' }}>{l.quantity_shipped}</td>
              <td colSpan={16}>
                <span className="pt-itemname">{l.sku} {l.item_name}</span>
              </td>
              <td colSpan={3}></td>
              <td className="pt-right" colSpan={5}>{l.preferred_bin_code || ''}</td>
              <td className="pt-right" colSpan={4}>{l.upc || ''}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="pt-tail">
        <div className="pt-returns">
          Returns or exchanges can be made up to 30 days after purchase. Go to www.avidmax.com/returns for details. Or email us at returns@avidmax.com.
        </div>

        <div className="pt-review">
          <img src="/picking-tickets/howdidwedo_block.jpg" alt="How Did We Do - Leave us a Google Review" />
        </div>

        <div className="pt-footer">
          <div>
            <BarcodeSvg
              value={orderNumber}
              className="pt-barcode-large"
              modulePx={1.2}
              height={40}
            />
          </div>
          <div></div>
          <div></div>
        </div>
      </div>
    </div>
  );
}

export default function PickingTicketPrint() {
  const { soId } = useParams();
  const [so, setSo] = useState(null);
  const [lines, setLines] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError('');
      const res = await api.get(`/admin/sales-orders/${soId}/picking-ticket`);
      if (cancelled) return;
      if (!res) {
        setError('Network error.');
        setLoading(false);
        return;
      }
      if (res.status === 404) {
        setError(`Sales order #${soId} not found.`);
        setLoading(false);
        return;
      }
      if (!res.ok) {
        setError(`Could not load sales order #${soId}.`);
        setLoading(false);
        return;
      }
      const data = await res.json();
      setSo(data.sales_order || null);
      setLines(data.lines || []);
      setLoading(false);
    }
    load();
    return () => { cancelled = true; };
  }, [soId]);

  if (loading) {
    return (
      <div className="pt-root">
        <div className="pt-toolbar pt-no-print">
          <Link to="/picking-tickets" className="pt-back">&larr; Back</Link>
        </div>
        <div className="pt-page">Loading ticket…</div>
      </div>
    );
  }

  if (error || !so) {
    return (
      <div className="pt-root">
        <div className="pt-toolbar pt-no-print">
          <Link to="/picking-tickets" className="pt-back">&larr; Back</Link>
        </div>
        <div className="pt-page">
          <h2>Could not render ticket</h2>
          <p>{error || 'Sales order data was empty.'}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="pt-root">
      <div className="pt-toolbar pt-no-print">
        <Link to="/picking-tickets" className="pt-back">&larr; Back</Link>
        <button onClick={() => window.print()}>Print</button>
      </div>
      <TicketDocument so={so} lines={lines} />
    </div>
  );
}
