import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

let mockUser = null;
let posActivitySetting = 'false';

vi.mock('../auth.jsx', () => ({
  useAuth: () => ({ user: mockUser }),
}));

vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1 }),
}));

vi.mock('../api.js', () => ({
  api: {
    get: vi.fn((path) => {
      if (path.startsWith('/admin/settings/pos_activity_enabled')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ value: posActivitySetting }),
        });
      }
      if (path.startsWith('/admin/dashboard')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({}),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    }),
  },
}));

import Sidebar, { NAV } from '../components/Sidebar.jsx';

async function renderSidebar() {
  let result;
  await act(async () => {
    result = render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );
  });
  return result;
}

describe('Sidebar navigation permissions filtering', () => {
  beforeEach(() => {
    posActivitySetting = 'false';
  });

  it('renders all nav groups and items for an ADMIN user (short-circuit)', async () => {
    mockUser = { role: 'ADMIN', username: 'admin' };
    await renderSidebar();

    // All section headers should be present
    expect(screen.getByText('Floor')).toBeInTheDocument();
    expect(screen.getByText('Inbound')).toBeInTheDocument();
    expect(screen.getByText('Outbound')).toBeInTheDocument();
    expect(screen.getByText('Warehouse')).toBeInTheDocument();
    expect(screen.getByText('System')).toBeInTheDocument();

    // Key items across sections should be present
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
    expect(screen.getByText('Inventory')).toBeInTheDocument();
    expect(screen.getByText('Counts')).toBeInTheDocument();
    expect(screen.getByText('Approvals')).toBeInTheDocument();
    expect(screen.getByText('Purchase Orders')).toBeInTheDocument();
    expect(screen.getByText('Receiving')).toBeInTheDocument();
    expect(screen.getByText('Put-away')).toBeInTheDocument();
    expect(screen.getByText('Sales Orders')).toBeInTheDocument();
    expect(screen.getByText('Fraud')).toBeInTheDocument();
    expect(screen.getByText('Backorders')).toBeInTheDocument();
    expect(screen.getByText('Items')).toBeInTheDocument();
    expect(screen.getByText('Data')).toBeInTheDocument();
    expect(screen.getByText('Users')).toBeInTheDocument();
    expect(screen.getByText('Settings')).toBeInTheDocument();
  });

  it('filters items for a USER with a partial grant', async () => {
    mockUser = {
      role: 'USER',
      username: 'operator',
      allowed_pages: ['inventory', 'receiving'],
    };
    await renderSidebar();

    // Items with no pageKey (Dashboard) stay visible
    expect(screen.getByText('Dashboard')).toBeInTheDocument();

    // Granted items are visible
    expect(screen.getByText('Inventory')).toBeInTheDocument();
    expect(screen.getByText('Receiving')).toBeInTheDocument();

    // Ungranted items in active groups are not visible
    expect(screen.queryByText('Counts')).not.toBeInTheDocument();
    expect(screen.queryByText('Approvals')).not.toBeInTheDocument();
    expect(screen.queryByText('Purchase Orders')).not.toBeInTheDocument();
    expect(screen.queryByText('Put-away')).not.toBeInTheDocument();

    // Groups containing granted items are visible
    expect(screen.getByText('Floor')).toBeInTheDocument();
    expect(screen.getByText('Inbound')).toBeInTheDocument();

    // Groups with no granted items are completely dropped
    expect(screen.queryByText('Outbound')).not.toBeInTheDocument();
    expect(screen.queryByText('Warehouse')).not.toBeInTheDocument();
    expect(screen.queryByText('System')).not.toBeInTheDocument();
  });

  it('matches pageKeys array on any key for the Data entry', async () => {
    // 1. With 'bins' granted, Data should appear under Warehouse
    mockUser = {
      role: 'USER',
      username: 'warehouse_staff',
      allowed_pages: ['bins'],
    };
    const { unmount } = await renderSidebar();

    expect(screen.getByText('Warehouse')).toBeInTheDocument();
    expect(screen.getByText('Data')).toBeInTheDocument();
    expect(screen.queryByText('Items')).not.toBeInTheDocument();
    expect(screen.queryByText('Vendors')).not.toBeInTheDocument();

    unmount();

    // 2. With 'zones' granted, Data should still appear
    mockUser = {
      role: 'USER',
      username: 'warehouse_staff',
      allowed_pages: ['zones'],
    };
    const { unmount: unmount2 } = await renderSidebar();
    expect(screen.getByText('Warehouse')).toBeInTheDocument();
    expect(screen.getByText('Data')).toBeInTheDocument();

    unmount2();

    // 3. Without any warehouse-layout pageKeys, Data is omitted and Warehouse group is dropped
    mockUser = {
      role: 'USER',
      username: 'sales_rep',
      allowed_pages: ['sales-orders'],
    };
    await renderSidebar();

    expect(screen.queryByText('Data')).not.toBeInTheDocument();
    expect(screen.queryByText('Warehouse')).not.toBeInTheDocument();
    expect(screen.getByText('Outbound')).toBeInTheDocument();
    expect(screen.getByText('Sales Orders')).toBeInTheDocument();
  });

  it('drops groups left with no items so no empty section headers render', async () => {
    mockUser = {
      role: 'USER',
      username: 'restricted_user',
      allowed_pages: ['purchase-orders'],
    };
    await renderSidebar();

    // Floor remains because Dashboard has no pageKey
    expect(screen.getByText('Floor')).toBeInTheDocument();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();

    // Inbound is kept because Purchase Orders is granted
    expect(screen.getByText('Inbound')).toBeInTheDocument();
    expect(screen.getByText('Purchase Orders')).toBeInTheDocument();

    // Groups with no allowed items must not render empty headers
    expect(screen.queryByText('Outbound')).not.toBeInTheDocument();
    expect(screen.queryByText('Warehouse')).not.toBeInTheDocument();
    expect(screen.queryByText('System')).not.toBeInTheDocument();
  });

  it('keeps Dashboard visible for any authenticated user even with no allowed_pages', async () => {
    mockUser = {
      role: 'USER',
      username: 'blank_user',
      allowed_pages: [],
    };
    await renderSidebar();

    // Floor renders with Dashboard only
    expect(screen.getByText('Floor')).toBeInTheDocument();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();

    // All other groups are empty and dropped
    expect(screen.queryByText('Inbound')).not.toBeInTheDocument();
    expect(screen.queryByText('Outbound')).not.toBeInTheDocument();
    expect(screen.queryByText('Warehouse')).not.toBeInTheDocument();
    expect(screen.queryByText('System')).not.toBeInTheDocument();
  });

  it('respects posActivityEnabled setting in combination with permissions', async () => {
    posActivitySetting = 'true';
    mockUser = { role: 'ADMIN', username: 'admin' };
    await renderSidebar();

    await waitFor(() => {
      expect(screen.getByText('POS Activity')).toBeInTheDocument();
    });
  });
});
