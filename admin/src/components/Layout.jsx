import { Outlet } from 'react-router-dom';
import TopBar from './TopBar.jsx';
import Sidebar from './Sidebar.jsx';

export default function Layout() {
  return (
    <div className="app-layout">
      <TopBar />
      <Sidebar />
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
