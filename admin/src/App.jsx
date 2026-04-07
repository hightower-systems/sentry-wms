import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuth } from './auth.jsx';
import Layout from './components/Layout.jsx';
import Login from './pages/Login.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Inventory from './pages/Inventory.jsx';
import CycleCounts from './pages/CycleCounts.jsx';
import Receiving from './pages/Receiving.jsx';
import PutAway from './pages/PutAway.jsx';
import Picking from './pages/Picking.jsx';
import Packing from './pages/Packing.jsx';
import Shipping from './pages/Shipping.jsx';
import Bins from './pages/Bins.jsx';
import Zones from './pages/Zones.jsx';
import Items from './pages/Items.jsx';
import Users from './pages/Users.jsx';
import AuditLog from './pages/AuditLog.jsx';
import PreferredBins from './pages/PreferredBins.jsx';
import Settings from './pages/Settings.jsx';

function ProtectedRoute({ children }) {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedRoute><Layout /></ProtectedRoute>}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/inventory" element={<Inventory />} />
        <Route path="/cycle-counts" element={<CycleCounts />} />
        <Route path="/receiving" element={<Receiving />} />
        <Route path="/putaway" element={<PutAway />} />
        <Route path="/picking" element={<Picking />} />
        <Route path="/packing" element={<Packing />} />
        <Route path="/shipping" element={<Shipping />} />
        <Route path="/bins" element={<Bins />} />
        <Route path="/zones" element={<Zones />} />
        <Route path="/items" element={<Items />} />
        <Route path="/preferred-bins" element={<PreferredBins />} />
        <Route path="/users" element={<Users />} />
        <Route path="/audit-log" element={<AuditLog />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
    </Routes>
  );
}
