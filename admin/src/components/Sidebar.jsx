import { NavLink, useLocation } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { api } from '../api.js';
import { useWarehouse } from '../warehouse.jsx';

const NAV = [
  {
    label: 'Floor',
    items: [
      { to: '/', label: 'Dashboard' },
      { to: '/inventory', label: 'Inventory' },
      { to: '/cycle-counts', label: 'Counts' },
      { to: '/count-approvals', label: 'Approvals' },
    ],
  },
  {
    label: 'Inbound',
    items: [
      { to: '/purchase-orders', label: 'Purchase Orders' },
      { to: '/receiving', label: 'Receiving' },
      { to: '/putaway', label: 'Put-away' },
    ],
  },
  {
    label: 'Outbound',
    items: [
      { to: '/sales-orders', label: 'Sales Orders' },
      { to: '/picking', label: 'Picking' },
      { to: '/packing', label: 'Packing' },
      { to: '/shipping', label: 'Shipping' },
    ],
  },
  {
    label: 'Warehouse',
    items: [
      { to: '/warehouses', label: 'Warehouses' },
      { to: '/bins', label: 'Bins' },
      { to: '/zones', label: 'Zones' },
      { to: '/items', label: 'Items' },
      { to: '/preferred-bins', label: 'Preferred Bins' },
      { to: '/adjustments', label: 'Adjustments' },
      { to: '/inter-warehouse-transfers', label: 'Transfers' },
    ],
  },
  {
    label: 'System',
    items: [
      { to: '/users', label: 'Users' },
      { to: '/api-tokens', label: 'API tokens' },
      { to: '/consumer-groups', label: 'Consumer groups' },
      { to: '/webhooks', label: 'Webhooks' },
      { to: '/audit-log', label: 'Audit log' },
      { to: '/imports', label: 'Import' },
      { to: '/integrations', label: 'Integrations' },
      { to: '/settings', label: 'Settings' },
    ],
  },
];

export default function Sidebar() {
  const location = useLocation();
  const { warehouseId } = useWarehouse();
  const [counts, setCounts] = useState({});

  useEffect(() => {
    if (!warehouseId) return;
    api.get(`/admin/dashboard?warehouse_id=${warehouseId}`).then(async (res) => {
      if (!res || !res.ok) return;
      const data = await res.json();
      setCounts({
        '/receiving': data.open_pos || 0,
        '/putaway': data.pending_putaway || 0,
        '/picking': data.orders_to_pick || 0,
        '/packing': data.orders_to_pack || 0,
        '/shipping': data.orders_to_ship || 0,
        '/count-approvals': data.pending_adjustments || 0,
      });
    });
  }, [location.pathname, warehouseId]);

  return (
    <nav className="sidebar">
      <div className="sidebar-wordmark">SENTRY</div>
      {NAV.map((group) => (
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
