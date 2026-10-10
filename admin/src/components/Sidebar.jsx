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
      { to: '/pos-activity', label: 'POS Activity' },
      { to: '/fraud', label: 'Fraud', pageKey: 'fraud' },
      // Partial-fulfill / backorders dashboard.
      { to: '/backorders', label: 'Backorders', pageKey: 'backorders' },
      { to: '/picking-tickets', label: 'Picking Tickets', pageKey: 'picking-tickets' },
      // Returns: RMA (the <orig>-RMA goods-in SOs) + Refunds (the <orig>-REFUND
      // credit memos) under one tabbed page. Reuses the sales-orders permission
      // -- the create-rma / receive-return / refund routes all gate on it.
      { to: '/returns', label: 'Returns', pageKey: 'sales-orders' },
      // Picking/Packing/Shipping are mobile-only: the admin-side
      // mirrors were retired. Supervisors use Sales Orders + Picking
      // Tickets here, and the handheld scanners drive the floor work.
      { to: '/picking-batches', label: 'Picking Batches', pageKey: 'picking-batches' },
    ],
  },
  {
    label: 'Warehouse',
    items: [
      // Day-to-day operational entries lead; the warehouse-layout
      // pages (Warehouses / Bins / Zones / Preferred Bins) collapse
      // into a single Data link with a tab strip.
      { to: '/items', label: 'Items', pageKey: 'items' },
      { to: '/vendors', label: 'Vendors', pageKey: 'vendors' },
      { to: '/adjustments', label: 'Inventory Adjustments', pageKey: 'adjustments' },
      { to: '/inter-warehouse-transfers', label: 'Inventory Transfers', pageKey: 'inter-warehouse-transfers' },
      { to: '/transfer-orders', label: 'Transfer Orders', pageKey: 'transfer-orders' },
      { to: '/data', label: 'Data', pageKeys: ['warehouses', 'bins', 'zones', 'preferred-bins'] },
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
      { to: '/channels', label: 'Channels', pageKey: 'channels' },
      // v1.13.0 mig 064 notification_webhooks. Sibling to /webhooks
      // because both are outbound-delivery surfaces; this one carries
      // chat-channel destinations (Teams adaptive cards), the other
      // signed-envelope HTTP subscribers.
      { to: '/notifications', label: 'Notifications', pageKey: 'notifications' },
      { to: '/audit-log', label: 'Audit log', pageKey: 'audit-log' },
      { to: '/imports', label: 'Import', pageKey: 'imports' },
      { to: '/integrations', label: 'Integrations', pageKey: 'integrations' },
      { to: '/settings', label: 'Settings', pageKey: 'settings' },
    ],
  },
];
export { NAV };

export default function Sidebar() {
  const location = useLocation();
  const { user } = useAuth();
  const { warehouseId } = useWarehouse();
  const [counts, setCounts] = useState({});
  // The POS Activity tab is opt-in via the pos_activity_enabled setting
  // so non-POS deployments never see it. Default false: the entry is
  // hidden until an operator turns it on in Settings > POS. Silent on
  // permission denial so a USER without the settings grant just gets
  // the default (hidden) rather than a Permissions Error popup.
  const [posActivityEnabled, setPosActivityEnabled] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.get(
      '/admin/settings/pos_activity_enabled',
      { silentPermissionDenied: true },
    ).then(async (res) => {
      if (!res?.ok || cancelled) return;
      const data = await res.json();
      setPosActivityEnabled(data?.value === 'true');
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
        '/count-approvals': data.pending_adjustments || 0,
        // /picking, /packing, /shipping badges were removed alongside
        // their retired nav entries; the throughput counts still live
        // on the Dashboard page itself.
        // v1.8.0 (#296): pending TO approvals scoped to the active
        // warehouse (source OR destination match). Falls back to 0
        // when the dashboard endpoint is the older shape.
        '/transfer-orders': data.pending_to_approvals || 0,
      });
    });
  }, [location.pathname, warehouseId]);

  const allowedPages = Array.isArray(user?.allowed_pages) ? user.allowed_pages : [];
  const isAdmin = user?.role === 'ADMIN';

  const permitted = isAdmin
    ? NAV
    : NAV.map((group) => ({
        ...group,
        items: group.items.filter((item) => {
          if (!item.pageKey && !item.pageKeys) return true;
          if (item.pageKey && allowedPages.includes(item.pageKey)) return true;
          if (Array.isArray(item.pageKeys) && item.pageKeys.some((k) => allowedPages.includes(k))) {
            return true;
          }
          return false;
        }),
      })).filter((group) => group.items.length > 0);

  const navGroups = (posActivityEnabled
    ? permitted
    : permitted.map((group) => ({
        ...group,
        items: group.items.filter((item) => item.to !== '/pos-activity'),
      }))
  ).filter((group) => group.items.length > 0);

  // Per-section collapse, persisted so a hidden section stays hidden across
  // reloads. The header carries a caret; clicking it toggles its items.
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem('sidebar.collapsed') || '{}');
    } catch {
      return {};
    }
  });
  function toggleGroup(label) {
    setCollapsed((c) => {
      const next = { ...c, [label]: !c[label] };
      try {
        localStorage.setItem('sidebar.collapsed', JSON.stringify(next));
      } catch {
        /* ignore quota / private-mode write failures */
      }
      return next;
    });
  }

  return (
    <nav className="sidebar">
      {navGroups.map((group) => {
        const isCollapsed = !!collapsed[group.label];
        return (
          <div key={group.label} className="sidebar-card">
            <div
              className="sidebar-group-label"
              role="button"
              tabIndex={0}
              aria-expanded={!isCollapsed}
              onClick={() => toggleGroup(group.label)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  toggleGroup(group.label);
                }
              }}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                cursor: 'pointer',
                userSelect: 'none',
              }}
            >
              <span>{group.label}</span>
              <span style={{ fontSize: 10, opacity: 0.7 }} aria-hidden>
                {isCollapsed ? '▸' : '▾'}
              </span>
            </div>
            {!isCollapsed && group.items.map((item) => (
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
        );
      })}
    </nav>
  );
}
