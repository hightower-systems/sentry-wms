import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuth } from './auth.jsx';
import ErrorBoundary from './components/ErrorBoundary.jsx';
import Layout from './components/Layout.jsx';
import Login from './pages/Login.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Inventory from './pages/Inventory.jsx';
import CycleCounts from './pages/CycleCounts.jsx';
import CycleCountApproval from './pages/CycleCountApproval.jsx';
import Receiving from './pages/Receiving.jsx';
import PurchaseOrders from './pages/PurchaseOrders.jsx';
import PutAway from './pages/PutAway.jsx';
import Picking from './pages/Picking.jsx';
import Packing from './pages/Packing.jsx';
import Shipping from './pages/Shipping.jsx';
import Bins from './pages/Bins.jsx';
import Zones from './pages/Zones.jsx';
import Items from './pages/Items.jsx';
import Warehouses from './pages/Warehouses.jsx';
import Users from './pages/Users.jsx';
import AuditLog from './pages/AuditLog.jsx';
import PreferredBins from './pages/PreferredBins.jsx';
import Settings from './pages/Settings.jsx';
import Imports from './pages/Imports.jsx';
import Integrations from './pages/Integrations.jsx';
import Adjustments from './pages/Adjustments.jsx';
import InterWarehouseTransfers from './pages/InterWarehouseTransfers.jsx';

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
        <Route path="/" element={<ErrorBoundary fallbackMessage="Could not load dashboard."><Dashboard /></ErrorBoundary>} />
        <Route path="/inventory" element={<ErrorBoundary fallbackMessage="Could not load inventory."><Inventory /></ErrorBoundary>} />
        <Route path="/cycle-counts" element={<ErrorBoundary fallbackMessage="Could not load cycle counts."><CycleCounts /></ErrorBoundary>} />
        <Route path="/count-approvals" element={<ErrorBoundary fallbackMessage="Could not load count approvals."><CycleCountApproval /></ErrorBoundary>} />
        <Route path="/receiving" element={<ErrorBoundary fallbackMessage="Could not load receiving."><Receiving /></ErrorBoundary>} />
        <Route path="/purchase-orders" element={<ErrorBoundary fallbackMessage="Could not load purchase orders."><PurchaseOrders /></ErrorBoundary>} />
        <Route path="/putaway" element={<ErrorBoundary fallbackMessage="Could not load put-away."><PutAway /></ErrorBoundary>} />
        <Route path="/picking" element={<ErrorBoundary fallbackMessage="Could not load picking."><Picking /></ErrorBoundary>} />
        <Route path="/packing" element={<ErrorBoundary fallbackMessage="Could not load packing."><Packing /></ErrorBoundary>} />
        <Route path="/shipping" element={<ErrorBoundary fallbackMessage="Could not load shipping."><Shipping /></ErrorBoundary>} />
        <Route path="/bins" element={<ErrorBoundary fallbackMessage="Could not load bins."><Bins /></ErrorBoundary>} />
        <Route path="/zones" element={<ErrorBoundary fallbackMessage="Could not load zones."><Zones /></ErrorBoundary>} />
        <Route path="/items" element={<ErrorBoundary fallbackMessage="Could not load items."><Items /></ErrorBoundary>} />
        <Route path="/warehouses" element={<ErrorBoundary fallbackMessage="Could not load warehouses."><Warehouses /></ErrorBoundary>} />
        <Route path="/preferred-bins" element={<ErrorBoundary fallbackMessage="Could not load preferred bins."><PreferredBins /></ErrorBoundary>} />
        <Route path="/users" element={<ErrorBoundary fallbackMessage="Could not load users."><Users /></ErrorBoundary>} />
        <Route path="/audit-log" element={<ErrorBoundary fallbackMessage="Could not load audit log."><AuditLog /></ErrorBoundary>} />
        <Route path="/settings" element={<ErrorBoundary fallbackMessage="Could not load settings."><Settings /></ErrorBoundary>} />
        <Route path="/imports" element={<ErrorBoundary fallbackMessage="Could not load imports."><Imports /></ErrorBoundary>} />
        <Route path="/integrations" element={<ErrorBoundary fallbackMessage="Could not load integrations."><Integrations /></ErrorBoundary>} />
        <Route path="/adjustments" element={<ErrorBoundary fallbackMessage="Could not load adjustments."><Adjustments /></ErrorBoundary>} />
        <Route path="/inter-warehouse-transfers" element={<ErrorBoundary fallbackMessage="Could not load transfers."><InterWarehouseTransfers /></ErrorBoundary>} />
      </Route>
    </Routes>
  );
}
