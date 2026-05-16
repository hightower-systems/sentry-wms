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
      { to: '/picking', label: 'Picking', pageKey: 'picking' },
      { to: '/packing', label: 'Packing', pageKey: 'packing' },
      { to: '/shipping', label: 'Shipping', pageKey: 'shipping' },
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
      { to: '/warehouses', label: 'Warehouses', pageKey: 'warehouses' },
      { to: '/bins', label: 'Bins', pageKey: 'bins' },
      { to: '/zones', label: 'Zones', pageKey: 'zones' },
      { to: '/preferred-bins', label: 'Preferred Bins', pageKey: 'preferred-bins' },
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
  // Mirrors the require_packing_before_shipping system setting. When
  // packing is disabled, the Packing nav entry is hidden so operators
  // do not navigate to a screen that no longer corresponds to any
  // active workflow. Default true matches Settings.jsx fallback.
  const [packingEnabled, setPackingEnabled] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // Background fetch - the Sidebar uses this to filter the Packing
    // entry but a USER who lacks the settings grant should not see
    // the global Permissions Error popup over it. silentPermissionDenied
    // suppresses the popup; the call still resolves and we fall back
    // to packingEnabled=true (default UI state).
    api.get(
      '/admin/settings/require_packing_before_shipping',
      { silentPermissionDenied: true },
    ).then(async (res) => {
      if (!res?.ok || cancelled) return;
      const data = await res.json();
      setPackingEnabled(data?.value !== 'false');
    }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!warehouseId) return;
    // Same silent treatment for the dashboard counts (sidebar badges).
    // /admin/dashboard is intentionally any-auth so this typically
    // succeeds, but a future tightening should not blow up the UI
    // with a modal on every page load.
    api.get(
      `/admin/dashboard?warehouse_id=${warehouseId}`,
      { silentPermissionDenied: true },
    ).then(async (res) => {
      if (!res || !res.ok) return;
      const data = await res.json();
      setCounts({
        '/receiving': data.open_pos || 0,
        '/putaway': data.pending_putaway || 0,
        '/picking': data.orders_to_pick || 0,
        '/packing': data.orders_to_pack || 0,
        '/shipping': data.orders_to_ship || 0,
        '/count-approvals': data.pending_adjustments || 0,
        // v1.8.0 (#296): pending TO approvals scoped to the active
        // warehouse (source OR destination match). Falls back to 0
        // when the dashboard endpoint is the older shape.
        '/transfer-orders': data.pending_to_approvals || 0,
      });
    });
  }, [location.pathname, warehouseId]);

  // ADMIN sees every page (allowed_pages is null/sentinel-style for
  // them on the backend, mirrored here as null). USERs only see
  // pages they have an explicit grant for; items without a pageKey
  // (e.g. Dashboard) always render.
  const allowedPages = user?.allowed_pages;
  const hasGrant = (item) => {
    if (!item.pageKey) return true;
    if (!allowedPages || allowedPages.includes('__admin__')) {
      // Defensive: server omits allowed_pages or sends sentinel.
      // Fall through to role check below.
    }
    if (user?.role === 'ADMIN') return true;
    return Array.isArray(allowedPages) && allowedPages.includes(item.pageKey);
  };

  const navGroups = (packingEnabled
    ? NAV
    : NAV.map((group) => ({
        ...group,
        items: group.items.filter((item) => item.to !== '/packing'),
      }))
  )
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
