import { NavLink, useLocation } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { api } from '../api.js';

const NAV = [
  {
    label: 'Floor',
    items: [
      { to: '/', label: 'Dashboard' },
      { to: '/inventory', label: 'Inventory' },
      { to: '/cycle-counts', label: 'Counts' },
    ],
  },
  {
    label: 'Inbound',
    items: [
      { to: '/receiving', label: 'Receiving' },
      { to: '/putaway', label: 'Put-away' },
    ],
  },
  {
    label: 'Outbound',
    items: [
      { to: '/picking', label: 'Picking' },
      { to: '/packing', label: 'Packing' },
      { to: '/shipping', label: 'Shipping' },
    ],
  },
  {
    label: 'Warehouse',
    items: [
      { to: '/bins', label: 'Bins' },
      { to: '/zones', label: 'Zones' },
      { to: '/items', label: 'Items' },
    ],
  },
  {
    label: 'System',
    items: [
      { to: '/users', label: 'Users' },
      { to: '/audit-log', label: 'Audit log' },
      { to: '/settings', label: 'Settings' },
    ],
  },
];

export default function Sidebar() {
  const location = useLocation();
  const [counts, setCounts] = useState({});

  useEffect(() => {
    api.get('/admin/dashboard?warehouse_id=1').then(async (res) => {
      if (!res || !res.ok) return;
      const data = await res.json();
      setCounts({
        '/receiving': data.open_pos || 0,
        '/putaway': data.pending_putaway || 0,
        '/picking': data.orders_to_pick || 0,
        '/packing': data.orders_to_pack || 0,
        '/shipping': data.orders_to_ship || 0,
      });
    });
  }, [location.pathname]);

  return (
    <nav className="sidebar">
      {NAV.map((group) => (
        <div key={group.label}>
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
