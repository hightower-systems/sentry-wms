import { useEffect, useRef, useState } from 'react';
import { Outlet } from 'react-router-dom';
import TopBar from './TopBar.jsx';
import Sidebar from './Sidebar.jsx';
import Modal from './Modal.jsx';
import { useAuth } from '../auth.jsx';

const PERM_POPUP_COOLDOWN_MS = 5000;

export default function Layout() {
  const { user } = useAuth();
  // avid-overhaul-mk1 P6.1: catch global permission-denied events from
  // api.js and surface a "Permissions Error" modal. Lives on Layout so
  // it covers every page reached through the admin shell.
  //
  // Two debouncers keep the modal from spamming a USER browsing pages
  // they have partial access to:
  //   1. Don't replace an already-open modal - the operator dismisses
  //      one at a time; piling new ones underneath is confusing.
  //   2. After dismiss, suppress further popups for COOLDOWN_MS so a
  //      page that fires several permission_denied calls during mount
  //      shows the modal once total, not once per call.
  const [permError, setPermError] = useState(null);
  const dismissedAt = useRef(0);

  useEffect(() => {
    function onPermDenied(evt) {
      const now = Date.now();
      if (now - dismissedAt.current < PERM_POPUP_COOLDOWN_MS) return;
      // Functional setter so we read the latest state without listing
      // permError as a deps dependency (which would re-bind the listener
      // every render).
      setPermError((current) => current || (evt.detail || { page_key: null }));
    }
    window.addEventListener('sentry:permission-denied', onPermDenied);
    return () => window.removeEventListener('sentry:permission-denied', onPermDenied);
  }, []);

  function dismissPermError() {
    dismissedAt.current = Date.now();
    setPermError(null);
  }

  // When the user is stuck in a forced-change flow the only available
  // actions are the change-password form and logout, so drop the sidebar
  // entirely and widen the main column.
  const forced = !!user?.must_change_password;
  return (
    <div className={`app-layout${forced ? ' forced-change' : ''}`}>
      <TopBar forced={forced} />
      {!forced && <Sidebar />}
      <main className="content">
        <Outlet />
      </main>
      {permError && (
        <Modal
          title="Permissions Error"
          onClose={dismissPermError}
          footer={
            <button className="btn btn-primary" onClick={dismissPermError}>
              OK
            </button>
          }
        >
          <p style={{ fontSize: 14, marginBottom: 12 }}>
            You do not have permission to access this resource.
          </p>
          {permError.page_key && (
            <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
              Page: <span className="mono">{permError.page_key}</span>
            </p>
          )}
          <p style={{ fontSize: 13 }}>
            Contact an administrator if you need access.
          </p>
        </Modal>
      )}
    </div>
  );
}
