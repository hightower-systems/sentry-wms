/**
 * v1.4.2 #100: regression lock for the useBlocker / data-router crash.
 *
 * #94 wired useDirtyFormGuard into Settings using react-router's
 * useBlocker. useBlocker is a data-router-only API (needs
 * createBrowserRouter + RouterProvider); the admin panel mounts under
 * the older declarative <BrowserRouter> + <Routes> pattern. The hook
 * threw on every Settings mount; the ErrorBoundary caught it; the
 * operator saw "Could not load settings." and had no way to change
 * any configuration. The #94 unit tests passed because they mocked
 * useBlocker directly, so the tests never exercised the hook against
 * the app's real router.
 *
 * This test mounts Settings inside the *actual* main.jsx router
 * shape -- <BrowserRouter> + <Routes> + <Route element={<Settings>}>
 * -- with no useBlocker mock. If a future change to the hook
 * reintroduces a data-router-only API, or any other router hook that
 * is not legal in this router shape, this test fails on render.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';

// Settings fetches /admin/warehouses/:id, settings keys, bins, and items.
// We stub the api surface to non-error responses; none of these paths
// are the target of this test. The target is the mount itself.
vi.mock('../api.js', () => ({
  api: {
    get: vi.fn(() => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) })),
    post: vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
    put: vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
    delete: vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({}) })),
  },
}));

vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1, warehouse: { warehouse_id: 1 } }),
}));

import Settings from '../pages/Settings.jsx';


describe('Settings page mounts inside main.jsx router shape (issue #100)', () => {
  let errorSpy;

  beforeEach(() => {
    // React logs thrown errors to console.error via its error-handling
    // machinery even when the error is caught by a boundary. Silence
    // during this test so a pass reads cleanly; a real failure still
    // surfaces via the thrown exception itself.
    errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('renders under <BrowserRouter><Routes> without throwing', async () => {
    expect(() => {
      render(
        <BrowserRouter>
          <Routes>
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Settings />} />
          </Routes>
        </BrowserRouter>,
      );
    }).not.toThrow();

    // Let any mount-time effects settle; React would surface a
    // thrown error from inside useEffect / render on the next tick.
    await waitFor(() => {
      const uncaught = errorSpy.mock.calls.find((call) =>
        call.some((arg) => String(arg).includes('useBlocker must be used within a data router')),
      );
      expect(uncaught, 'useBlocker data-router crash must not recur').toBeUndefined();
    });
  });
});
