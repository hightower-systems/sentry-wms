import { NavLink, useLocation } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useAuth } from '../auth.jsx';
import { useWarehouse } from '../warehouse.jsx';

// Each NAV item carries a page_key matching api/constants.py
// ALL_PAGE_KEYS. Sidebar filters by the user's allowed_pages so a
// USER without the grant never sees the link (and the backend
// rejects direct URL hits via @require_admin_or_page_permission).
// Dashboard intentionally has no page_key: it is reachable by any
// authenticated user so the badges below populate.
const NAV = [
  {
    label: 'Floor',
    items: [
      { to: '/', label: 'Dashboard' },
      { to: '/inventory', label: 'Inventory', pageKey: 'inventory' },
      { to: '/cycle-counts', label: 'Counts', pageKey: 'cycle-counts' },
      { to: '/count-approvals', label: 'Approvals', pageKey: 'count-approvals' },
    ],
  },
  {
    label: 'Inbound',
    items: [
      { to: '/purchase-orders', label: 'Purchase Orders', pageKey: 'purchase-orders' },
      { to: '/receiving', label: 'Receiving', pageKey: 'receiving' },
      { to: '/putaway', label: 'Put-away', pageKey: 'putaway' },
    ],
  },
  {
    label: 'Outbound',
    items: [
      { to: '/sales-orders', label: 'Sales Orders', pageKey: 'sales-orders' },
      { to: '/pos-activity', label: 'POS Activity', pageKey: 'pos-activity' },
      { to: '/picking-tickets', label: 'Picking Tickets', pageKey: 'picking-tickets' },
      { to: '/fraud', label: 'Fraud', pageKey: 'fraud' },
      // avid-overhaul-mk1 P10: Picking/Packing/Shipping mobile-only.
      // The admin-side mirrors were retired; supervisors use Sales
      // Orders + Picking Tickets here and the C6000 scanners handle
      // the actual floor workflow.
    ],
  },
  {
    label: 'Warehouse',
    items: [
      { to: '/items', label: 'Items', pageKey: 'items' },
      { to: '/vendors', label: 'Vendors', pageKey: 'vendors' },
      { to: '/adjustments', label: 'Inventory Adjustments', pageKey: 'adjustments' },
      { to: '/inter-warehouse-transfers', label: 'Inventory Transfers', pageKey: 'inter-warehouse-transfers' },
      { to: '/transfer-orders', label: 'Transfer Orders', pageKey: 'transfer-orders' },
      // avid-overhaul-mk1 P8: Warehouses/Bins/Zones/Preferred Bins
      // collapsed into a single /data page with tabs. pageKeys (plural)
      // means "show this link if the user holds at least one of these"
      // - same backend gates still apply per tab inside /data.
      {
        to: '/data',
        label: 'Data',
        pageKeys: ['warehouses', 'bins', 'zones', 'preferred-bins'],
      },
    ],
  },
  {
    label: 'System',
    items: [
      { to: '/users', label: 'Users', pageKey: 'users' },
      { to: '/api-tokens', label: 'API tokens', pageKey: 'api-tokens' },
      { to: '/inbound', label: 'Inbound activity', pageKey: 'inbound' },
      { to: '/consumer-groups', label: 'Consumer groups', pageKey: 'consumer-groups' },
      { to: '/webhooks', label: 'Webhooks', pageKey: 'webhooks' },
      { to: '/audit-log', label: 'Audit log', pageKey: 'audit-log' },
      { to: '/imports', label: 'Import', pageKey: 'imports' },
      { to: '/integrations', label: 'Integrations', pageKey: 'integrations' },
      { to: '/settings', label: 'Settings', pageKey: 'settings' },
    ],
  },
];

export default function Sidebar() {
  const location = useLocation();
  const { user } = useAuth();
  const { warehouseId } = useWarehouse();
  const [counts, setCounts] = useState({});

  useEffect(() => {
    if (!warehouseId) return;
    // /admin/dashboard is intentionally any-auth so the badge counts
    // populate without an extra permission grant. silentPermissionDenied
    // is left in place so a future tightening of the gate cannot blow
    // up the UI with a modal on every page load.
    api.get(
      `/admin/dashboard?warehouse_id=${warehouseId}`,
      { silentPermissionDenied: true },
    ).then(async (res) => {
      if (!res || !res.ok) return;
      const data = await res.json();
      setCounts({
        '/receiving': data.open_pos || 0,
        '/putaway': data.pending_putaway || 0,
        '/count-approvals': data.pending_adjustments || 0,
        // v1.8.0 (#296): pending TO approvals scoped to the active
        // warehouse (source OR destination match). Falls back to 0
        // when the dashboard endpoint is the older shape.
        '/transfer-orders': data.pending_to_approvals || 0,
        // avid-overhaul-mk1 P10: /picking, /packing, /shipping badges
        // removed alongside their nav entries. The throughput counts
        // still live on the Dashboard page itself.
      });
    });
  }, [location.pathname, warehouseId]);

  // ADMIN sees every page (allowed_pages is null/sentinel-style for
  // them on the backend, mirrored here as null). USERs only see
  // pages they have an explicit grant for; items without a pageKey
  // (e.g. Dashboard) always render. P8 introduces items with
  // `pageKeys` (plural) for grouped pages like /data - the link
  // shows when the user holds at least one of the listed keys, and
  // the Data page itself hides the tabs the user lacks.
  const allowedPages = user?.allowed_pages;
  const hasGrant = (item) => {
    if (!item.pageKey && !item.pageKeys) return true;
    if (user?.role === 'ADMIN') return true;
    if (!Array.isArray(allowedPages)) return false;
    if (item.pageKeys) {
      return item.pageKeys.some((k) => allowedPages.includes(k));
    }
    return allowedPages.includes(item.pageKey);
  };

  const navGroups = NAV
    .map((group) => ({ ...group, items: group.items.filter(hasGrant) }))
    .filter((group) => group.items.length > 0);

  return (
    <nav className="sidebar">
      {navGroups.map((group) => (
        <div key={group.label} className="sidebar-card">
          <div className="sidebar-group-label">{group.label}</div>
          {group.items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                `sidebar-link${isActive ? ' active' : ''}`
              }
            >
              <span>{item.label}</span>
              {counts[item.to] > 0 && (
                <span className="sidebar-badge">{counts[item.to]}</span>
              )}
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}
