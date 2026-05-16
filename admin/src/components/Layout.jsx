import { useEffect, useState } from 'react';
import { Outlet } from 'react-router-dom';
import TopBar from './TopBar.jsx';
import Sidebar from './Sidebar.jsx';
import Modal from './Modal.jsx';
import { useAuth } from '../auth.jsx';

export default function Layout() {
  const { user } = useAuth();
  // avid-overhaul-mk1 P6.1: catch global permission-denied events from
  // api.js and surface a "Permissions Error" modal. Lives on Layout so
  // it covers every page reached through the admin shell.
  const [permError, setPermError] = useState(null);

  useEffect(() => {
    function onPermDenied(evt) {
      setPermError(evt.detail || { page_key: null });
    }
    window.addEventListener('sentry:permission-denied', onPermDenied);
    return () => window.removeEventListener('sentry:permission-denied', onPermDenied);
  }, []);

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
          onClose={() => setPermError(null)}
          footer={
            <button className="btn btn-primary" onClick={() => setPermError(null)}>
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
