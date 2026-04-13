/**
 * Admin panel auth flow tests.
 *
 * These catch the two bugs that caused the infinite reload loop:
 * 1. api.js must clear BOTH sentry_token and sentry_user on 401
 * 2. WarehouseProvider must not fetch when no user is logged in
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../auth.jsx';
import { WarehouseProvider } from '../warehouse.jsx';
import App from '../App.jsx';

// Stub window.location to prevent jsdom errors on redirect
const locationAssignSpy = vi.fn();
Object.defineProperty(window, 'location', {
  value: { href: '', assign: locationAssignSpy },
  writable: true,
});

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.restoreAllMocks();
  window.location.href = '';
});

function renderApp(initialRoute = '/') {
  return render(
    <MemoryRouter initialEntries={[initialRoute]}>
      <AuthProvider>
        <WarehouseProvider>
          <App />
        </WarehouseProvider>
      </AuthProvider>
    </MemoryRouter>
  );
}

// -- Test 1: Login page renders without API calls when not authenticated ------

describe('unauthenticated access', () => {
  it('renders login page without making API calls', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    renderApp('/login');

    await waitFor(() => {
      expect(screen.getByText('Sign in')).toBeInTheDocument();
    });

    // No API calls should have been made - WarehouseProvider should skip fetch
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('redirects to login when accessing protected route', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    renderApp('/');

    await waitFor(() => {
      expect(screen.getByText('Sign in')).toBeInTheDocument();
    });

    // Still no API calls - redirect happened via React Router, not 401 loop
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

// -- Test 2: 401 response clears all auth state ------------------------------

describe('401 handling', () => {
  it('clears both token and user from localStorage on 401', async () => {
    // Simulate a logged-in state with a stale token
    localStorage.setItem('sentry_token', 'stale-token');
    localStorage.setItem('sentry_user', JSON.stringify({
      user_id: 1, username: 'admin', role: 'ADMIN',
    }));

    // Mock fetch to return 401 for any API call
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      status: 401,
      ok: false,
      json: async () => ({ error: 'Token expired' }),
    });

    // Import and call api directly to test the 401 handler
    const { api } = await import('../api.js');
    await api.get('/admin/dashboard');

    expect(localStorage.getItem('sentry_token')).toBeNull();
    expect(localStorage.getItem('sentry_user')).toBeNull();
  });

  it('does not redirect on 401 from login endpoint', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      status: 401,
      ok: false,
      json: async () => ({ error: 'Invalid credentials' }),
    });

    const { api } = await import('../api.js');
    const res = await api.post('/auth/login', {
      username: 'bad', password: 'creds',
    });

    // Login 401s should return the response, not redirect
    expect(res).toBeDefined();
    expect(res.status).toBe(401);
    // Token should NOT be cleared for login failures
    expect(window.location.href).not.toBe('/login');
  });
});

// -- Test 3: WarehouseProvider only fetches when user exists ------------------

describe('WarehouseProvider', () => {
  it('does not fetch warehouses when no user is logged in', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    render(
      <MemoryRouter>
        <AuthProvider>
          <WarehouseProvider>
            <div data-testid="child">loaded</div>
          </WarehouseProvider>
        </AuthProvider>
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByTestId('child')).toBeInTheDocument();
    });

    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('fetches warehouses when user is logged in', async () => {
    localStorage.setItem('sentry_token', 'valid-token');
    localStorage.setItem('sentry_user', JSON.stringify({
      user_id: 1, username: 'admin', role: 'ADMIN',
    }));

    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({ warehouses: [{ id: 1, warehouse_code: 'WH1', warehouse_name: 'Main' }] }),
    });

    render(
      <MemoryRouter>
        <AuthProvider>
          <WarehouseProvider>
            <div data-testid="child">loaded</div>
          </WarehouseProvider>
        </AuthProvider>
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/api/admin/warehouses',
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: 'Bearer valid-token',
          }),
        }),
      );
    });
  });
});
