import { useState, useRef, useEffect } from 'react';
import { useAuth } from '../auth.jsx';

export default function TopBar() {
  const { user, logout } = useAuth();
  const [showMenu, setShowMenu] = useState(false);
  const menuRef = useRef(null);

  const initials = user?.full_name
    ? user.full_name.split(' ').map(n => n[0]).join('').toUpperCase()
    : user?.username?.[0]?.toUpperCase() || '?';

  useEffect(() => {
    function handleClickOutside(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setShowMenu(false);
      }
    }
    if (showMenu) document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showMenu]);

  return (
    <div className="topbar">
      <div className="topbar-logo">
        <svg width="22" height="22" viewBox="0 0 32 32">
          <rect x="1" y="1" width="30" height="30" rx="5" fill="#8e2715"/>
          <rect x="7" y="6" width="7.5" height="20" rx="1.5" fill="none" stroke="#FCF4E3" strokeWidth="1.6"/>
          <rect x="17.5" y="6" width="7.5" height="20" rx="1.5" fill="none" stroke="#FCF4E3" strokeWidth="1.6"/>
          <line x1="8.5" y1="12" x2="13" y2="12" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
          <line x1="8.5" y1="16" x2="13" y2="16" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
          <line x1="8.5" y1="20" x2="13" y2="20" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
          <line x1="19" y1="12" x2="23.5" y2="12" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
          <line x1="19" y1="16" x2="23.5" y2="16" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
          <line x1="19" y1="20" x2="23.5" y2="20" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
        </svg>
        Sentry WMS
      </div>
      <div className="topbar-breadcrumb">
        <span>/</span> APT-LAB
      </div>
      <div className="topbar-search">
        <input type="text" placeholder="Search items, bins, orders..." />
      </div>
      <div className="topbar-user" ref={menuRef} style={{ position: 'relative' }}>
        <div className="topbar-avatar" onClick={() => setShowMenu(!showMenu)} title={user?.full_name || user?.username}>
          {initials}
        </div>
        {showMenu && (
          <div className="topbar-dropdown">
            <div className="topbar-dropdown-header">
              <div style={{ fontWeight: 600, fontSize: 13 }}>{user?.full_name || user?.username}</div>
              <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{user?.role}</div>
            </div>
            <div className="topbar-dropdown-divider" />
            <button className="topbar-dropdown-item" onClick={logout}>
              Logout
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
