/**
 * Admin panel auth flow tests (V-045 cookie-based).
 *
 * After V-045 the admin SPA no longer stores tokens in localStorage.
 * Session lives in an HttpOnly cookie; the bootstrap flow asks
 * /api/auth/me to determine whether the user is logged in, and all
 * mutating requests attach an X-CSRF-Token header read from the
 * readable sentry_csrf cookie.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../auth.jsx';
import { WarehouseProvider } from '../warehouse.jsx';
import App from '../App.jsx';

const locationAssignSpy = vi.fn();
Object.defineProperty(window, 'location', {
  value: { href: '', assign: locationAssignSpy },
  writable: true,
});

function mockMeResponse({ status = 401, body = null } = {}) {
  return vi.fn().mockImplementation((input) => {
    const url = typeof input === 'string' ? input : input.url;
    if (url.includes('/api/auth/me')) {
      return Promise.resolve({
        status,
        ok: status >= 200 && status < 300,
        json: async () => body || { error: 'Unauthorized' },
      });
    }
    return Promise.resolve({ status: 200, ok: true, json: async () => ({}) });
  });
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  document.cookie.split(';').forEach((c) => {
    const name = c.split('=')[0].trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  });
  vi.restoreAllMocks();
  window.location.href = '';
});

// -- Bootstrap: no session cookie -> stays unauthenticated --------------------

describe('unauthenticated bootstrap', () => {
  it('renders login page and calls /auth/me once, no other API calls', async () => {
    const fetchSpy = mockMeResponse({ status: 401 });
    vi.stubGlobal('fetch', fetchSpy);

    render(
      <MemoryRouter initialEntries={['/login']}>
        <AuthProvider>
          <WarehouseProvider>
            <App />
          </WarehouseProvider>
        </AuthProvider>
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText('Sign in')).toBeInTheDocument();
    });

    const urls = fetchSpy.mock.calls.map(([u]) => (typeof u === 'string' ? u : u.url));
    expect(urls.some((u) => u.includes('/api/auth/me'))).toBe(true);
    expect(urls.some((u) => u.includes('/api/admin/warehouses'))).toBe(false);
  });

  it('redirects to login when accessing protected route without a session', async () => {
    vi.stubGlobal('fetch', mockMeResponse({ status: 401 }));

    render(
      <MemoryRouter initialEntries={['/']}>
        <AuthProvider>
          <WarehouseProvider>
            <App />
          </WarehouseProvider>
        </AuthProvider>
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText('Sign in')).toBeInTheDocument();
    });
  });
});

// -- API client: credentials + CSRF --------------------------------------------

describe('api client', () => {
  it('sends credentials: include on every request', async () => {
    const fetchSpy = vi.fn().mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({}),
    });
    vi.stubGlobal('fetch', fetchSpy);

    const { api } = await import('../api.js');
    await api.get('/admin/dashboard');

    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/admin/dashboard',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('attaches X-CSRF-Token header on POST when the CSRF cookie is set', async () => {
    document.cookie = 'sentry_csrf=test-csrf-value-123; path=/';
    const fetchSpy = vi.fn().mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({}),
    });
    vi.stubGlobal('fetch', fetchSpy);

    const { api } = await import('../api.js');
    await api.post('/admin/connectors/test', { name: 'x' });

    const [, options] = fetchSpy.mock.calls[0];
    expect(options.headers['X-CSRF-Token']).toBe('test-csrf-value-123');
    expect(options.credentials).toBe('include');
  });

  it('does not attach Authorization header (token no longer in localStorage)', async () => {
    // Even if some legacy code path tried to set this, the current api.js
    // never reads localStorage. Verify the header is absent.
    const fetchSpy = vi.fn().mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({}),
    });
    vi.stubGlobal('fetch', fetchSpy);

    const { api } = await import('../api.js');
    await api.get('/admin/dashboard');

    const [, options] = fetchSpy.mock.calls[0];
    expect(options.headers.Authorization).toBeUndefined();
  });

  it('does not attach CSRF header on GET requests', async () => {
    document.cookie = 'sentry_csrf=csrf-v; path=/';
    const fetchSpy = vi.fn().mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => ({}),
    });
    vi.stubGlobal('fetch', fetchSpy);

    const { api } = await import('../api.js');
    await api.get('/admin/dashboard');

    const [, options] = fetchSpy.mock.calls[0];
    expect(options.headers['X-CSRF-Token']).toBeUndefined();
  });
});

// -- 401 handling --------------------------------------------------------------

describe('401 handling', () => {
  it('redirects to /login on 401 from a protected endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status: 401,
      ok: false,
      json: async () => ({ error: 'Unauthorized' }),
    }));

    const { api } = await import('../api.js');
    await api.get('/admin/dashboard');

    expect(window.location.href).toBe('/login');
  });

  it('does not redirect on 401 from /auth/login itself', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status: 401,
      ok: false,
      json: async () => ({ error: 'Invalid credentials' }),
    }));

    const { api } = await import('../api.js');
    const res = await api.post('/auth/login', {
      username: 'bad', password: 'creds',
    });

    expect(res).toBeDefined();
    expect(res.status).toBe(401);
    expect(window.location.href).not.toBe('/login');
  });

  it('does not redirect on 401 from /auth/me (bootstrap probe)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status: 401,
      ok: false,
      json: async () => ({ error: 'Unauthorized' }),
    }));

    const { api } = await import('../api.js');
    const res = await api.get('/auth/me');

    expect(res).toBeDefined();
    expect(res.status).toBe(401);
    expect(window.location.href).not.toBe('/login');
  });
});

// -- WarehouseProvider gating --------------------------------------------------

describe('WarehouseProvider', () => {
  it('does not fetch warehouses when the bootstrap returns no user', async () => {
    const fetchSpy = mockMeResponse({ status: 401 });
    vi.stubGlobal('fetch', fetchSpy);

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

    const urls = fetchSpy.mock.calls.map(([u]) => (typeof u === 'string' ? u : u.url));
    expect(urls.some((u) => u.includes('/api/admin/warehouses'))).toBe(false);
  });

  it('fetches warehouses after bootstrap returns an ADMIN user', async () => {
    const fetchSpy = vi.fn().mockImplementation((input) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/api/auth/me')) {
        return Promise.resolve({
          status: 200, ok: true,
          json: async () => ({ user_id: 1, username: 'admin', role: 'ADMIN' }),
        });
      }
      if (url.includes('/api/admin/warehouses')) {
        return Promise.resolve({
          status: 200, ok: true,
          json: async () => ({ warehouses: [{ id: 1, warehouse_code: 'WH1', warehouse_name: 'Main' }] }),
        });
      }
      return Promise.resolve({ status: 200, ok: true, json: async () => ({}) });
    });
    vi.stubGlobal('fetch', fetchSpy);

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
      expect(fetchSpy).toHaveBeenCalledWith(
        '/api/admin/warehouses',
        expect.objectContaining({ credentials: 'include' }),
      );
    });

    // Ensure no Authorization header was attached
    const warehouseCall = fetchSpy.mock.calls.find(
      ([u]) => (typeof u === 'string' ? u : u.url) === '/api/admin/warehouses',
    );
    expect(warehouseCall[1].headers.Authorization).toBeUndefined();
  });
});
