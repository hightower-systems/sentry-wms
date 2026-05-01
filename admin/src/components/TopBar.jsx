import { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../auth.jsx';
import { useWarehouse } from '../warehouse.jsx';
import { api } from '../api.js';

const SEARCH_DEBOUNCE_MS = 250;

const RESULT_TYPE_LABEL = {
  item: 'Item',
  bin: 'Bin',
  po: 'PO',
  so: 'SO',
  customer: 'Customer',
};

function resultRoute(r) {
  // Selection routes the operator to the list page filtered by the
  // result's primary label. Item / bin / PO / SO map directly to
  // their list page; customer maps to the sales-orders list since
  // there is no customers detail view.
  const q = encodeURIComponent(r.label);
  switch (r.type) {
    case 'item': return `/items?q=${q}`;
    case 'bin': return `/bins?q=${q}`;
    case 'po': return `/purchase-orders?q=${q}`;
    case 'so': return `/sales-orders?q=${q}`;
    case 'customer': return `/sales-orders?q=${q}`;
    default: return '/';
  }
}

export default function TopBar({ forced = false }) {
  const { user, logout } = useAuth();
  const { warehouses, warehouseId, warehouse, setWarehouseId } = useWarehouse();
  const navigate = useNavigate();
  const [showMenu, setShowMenu] = useState(false);
  const [showWhPicker, setShowWhPicker] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchHighlight, setSearchHighlight] = useState(-1);
  const menuRef = useRef(null);
  const whRef = useRef(null);
  const searchRef = useRef(null);

  const initials = user?.full_name
    ? user.full_name.split(' ').map(n => n[0]).join('').toUpperCase()
    : user?.username?.[0]?.toUpperCase() || '?';

  useEffect(() => {
    function handleClickOutside(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setShowMenu(false);
      }
      if (whRef.current && !whRef.current.contains(e.target)) {
        setShowWhPicker(false);
      }
      if (searchRef.current && !searchRef.current.contains(e.target)) {
        setSearchOpen(false);
      }
    }
    if (showMenu || showWhPicker || searchOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showMenu, showWhPicker, searchOpen]);

  useEffect(() => {
    const q = searchQuery.trim();
    const handle = setTimeout(async () => {
      if (q.length < 2) {
        setSearchResults([]);
        setSearchLoading(false);
        return;
      }
      setSearchLoading(true);
      const params = new URLSearchParams({ q });
      if (warehouseId) params.set('warehouse_id', String(warehouseId));
      const res = await api.get(`/admin/search?${params}`);
      if (res?.ok) {
        const data = await res.json();
        setSearchResults(data.results || []);
        setSearchHighlight(-1);
      } else {
        setSearchResults([]);
      }
      setSearchLoading(false);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [searchQuery, warehouseId]);

  function selectSearchResult(r) {
    if (!r) return;
    navigate(resultRoute(r));
    setSearchOpen(false);
    setSearchQuery('');
    setSearchResults([]);
    setSearchHighlight(-1);
  }

  function handleSearchKeyDown(e) {
    if (!searchOpen || searchResults.length === 0) {
      if (e.key === 'Escape') setSearchOpen(false);
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSearchHighlight((i) => Math.min(searchResults.length - 1, i + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSearchHighlight((i) => Math.max(-1, i - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const idx = searchHighlight >= 0 ? searchHighlight : 0;
      selectSearchResult(searchResults[idx]);
    } else if (e.key === 'Escape') {
      setSearchOpen(false);
    }
  }

  function selectWarehouse(id) {
    setWarehouseId(id);
    setShowWhPicker(false);
  }

  const whCode = warehouse?.warehouse_code || warehouse?.code || '...';

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
      {!forced && <div className="topbar-breadcrumb" ref={whRef} style={{ position: 'relative' }}>
        <span
          className="topbar-wh-picker"
          onClick={() => setShowWhPicker(!showWhPicker)}
        >
          <span>/</span> {whCode}
          <svg width="10" height="10" viewBox="0 0 10 10" style={{ marginLeft: 4, opacity: 0.5 }}>
            <path d="M2 4 L5 7 L8 4" stroke="currentColor" strokeWidth="1.2" fill="none" strokeLinecap="round"/>
          </svg>
        </span>
        {showWhPicker && warehouses.length > 0 && (
          <div className="topbar-wh-dropdown">
            {warehouses.map((w) => {
              const wId = w.warehouse_id || w.id;
              const isActive = wId === warehouseId;
              return (
                <div
                  key={wId}
                  className={`topbar-wh-option${isActive ? ' active' : ''}`}
                  onClick={() => selectWarehouse(wId)}
                >
                  <span className="topbar-wh-code">{w.warehouse_code || w.code}</span>
                  <span className="topbar-wh-name">{w.warehouse_name || w.name}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>}
      {!forced && <div className="topbar-search" ref={searchRef} style={{ position: 'relative' }}>
        <input
          type="text"
          placeholder="Search items, bins, orders..."
          value={searchQuery}
          onChange={(e) => { setSearchQuery(e.target.value); setSearchOpen(true); }}
          onFocus={() => { if (searchQuery.trim().length >= 2) setSearchOpen(true); }}
          onKeyDown={handleSearchKeyDown}
        />
        {searchOpen && searchQuery.trim().length >= 2 && (
          <div className="topbar-wh-dropdown" style={{ minWidth: 320, maxHeight: 360, overflowY: 'auto' }}>
            {searchLoading && (
              <div className="topbar-wh-option" style={{ color: 'rgba(255,255,255,0.5)', cursor: 'default' }}>
                Searching…
              </div>
            )}
            {!searchLoading && searchResults.length === 0 && (
              <div className="topbar-wh-option" style={{ color: 'rgba(255,255,255,0.5)', cursor: 'default' }}>
                No matches
              </div>
            )}
            {!searchLoading && searchResults.map((r, idx) => {
              const key = `${r.type}-${r.id}`;
              const isActive = idx === searchHighlight;
              return (
                <div
                  key={key}
                  className={`topbar-wh-option${isActive ? ' active' : ''}`}
                  onMouseEnter={() => setSearchHighlight(idx)}
                  onClick={() => selectSearchResult(r)}
                >
                  <span className="topbar-wh-code">[{RESULT_TYPE_LABEL[r.type] || r.type}] {r.label}</span>
                  {r.sublabel && <span className="topbar-wh-name">{r.sublabel}</span>}
                </div>
              );
            })}
          </div>
        )}
      </div>}
      <div className="topbar-user" ref={menuRef} style={{ position: 'relative' }}>
        <div className="topbar-avatar" onClick={() => setShowMenu(!showMenu)} title={user?.full_name || user?.username}>
          {initials}
        </div>
        {showMenu && (
          <div className="topbar-dropdown">
            <div className="topbar-dropdown-header">
              <div style={{ fontWeight: 600, fontSize: 13, color: '#fdf4e3' }}>{user?.full_name || user?.username}</div>
              <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.5)' }}>{user?.role}</div>
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
