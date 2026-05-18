import { NavLink, Outlet, Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../auth.jsx';

// avid-overhaul-mk1 P8: warehouse-layout pages (Warehouses / Bins /
// Zones / Preferred Bins) consolidated under a single /data parent
// with a tab strip. The sub-pages render as Outlet children so each
// one keeps its own PageHeader + state without any changes; the only
// new chrome is the tab strip below.
//
// Per-tab grants honor the existing page_keys so a USER with only
// 'bins' sees the bins tab and is redirected to it on /data. ADMIN
// sees every tab. Backend endpoints stay on their per-entity page
// permission so granular access keeps working.

const TABS = [
  { to: 'warehouses', label: 'Warehouses', pageKey: 'warehouses' },
  { to: 'bins', label: 'Bins', pageKey: 'bins' },
  { to: 'zones', label: 'Zones', pageKey: 'zones' },
  { to: 'preferred-bins', label: 'Preferred Bins', pageKey: 'preferred-bins' },
];

export default function Data() {
  const { user } = useAuth();
  const location = useLocation();
  const allowedPages = user?.allowed_pages;
  const isAdmin = user?.role === 'ADMIN';

  const visibleTabs = TABS.filter((t) =>
    isAdmin || (Array.isArray(allowedPages) && allowedPages.includes(t.pageKey)),
  );

  // /data with no sub-route lands on the first tab the user can see.
  // A USER with no grants in this section gets a clear "no access"
  // message instead of an empty page.
  if (location.pathname === '/data' || location.pathname === '/data/') {
    if (visibleTabs.length === 0) {
      return (
        <div style={{ padding: 24, color: 'var(--text-secondary)' }}>
          No warehouse-data pages granted to your account.
        </div>
      );
    }
    return <Navigate to={visibleTabs[0].to} replace />;
  }

  return (
    <div>
      <div className="data-tabs">
        {visibleTabs.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            className={({ isActive }) =>
              `data-tab${isActive ? ' active' : ''}`
            }
          >
            {tab.label}
          </NavLink>
        ))}
      </div>
      <Outlet />
    </div>
  );
}
