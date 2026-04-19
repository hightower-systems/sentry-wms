import { Outlet } from 'react-router-dom';
import TopBar from './TopBar.jsx';
import Sidebar from './Sidebar.jsx';
import { useAuth } from '../auth.jsx';

export default function Layout() {
  const { user } = useAuth();
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
    </div>
  );
}
