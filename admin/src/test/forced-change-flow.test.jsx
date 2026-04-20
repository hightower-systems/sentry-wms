/**
 * v1.4.1 forced password change: admin panel router guard and
 * forced-mode UI behaviour (issue #69).
 *
 * The server-side middleware already blocks everything but three
 * endpoints when must_change_password=true. These tests cover the
 * client-side experience:
 *  - ProtectedRoute redirects any other path to /change-password
 *    while the flag is set.
 *  - The change-password page renders the forced-mode banner and
 *    hides Cancel.
 *  - Layout hides the sidebar in forced mode.
 *  - A normal user (flag=false) still sees the regular shell.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from '../auth.jsx';
import { WarehouseProvider } from '../warehouse.jsx';
import App from '../App.jsx';

Object.defineProperty(window, 'location', {
  value: { href: '', assign: vi.fn() },
  writable: true,
});

function mockFetch({ meBody, meStatus = 200, dashboardBody = {} }) {
  return vi.fn().mockImplementation((input) => {
    const url = typeof input === 'string' ? input : input.url;
    if (url.includes('/api/auth/me')) {
      return Promise.resolve({
        status: meStatus,
        ok: meStatus >= 200 && meStatus < 300,
        json: async () => meBody,
      });
    }
    if (url.includes('/api/admin/warehouses')) {
      return Promise.resolve({
        status: 200,
        ok: true,
        json: async () => ({
          warehouses: [{ warehouse_id: 1, warehouse_code: 'WH1', warehouse_name: 'Main' }],
        }),
      });
    }
    if (url.includes('/api/admin/dashboard')) {
      return Promise.resolve({ status: 200, ok: true, json: async () => dashboardBody });
    }
    return Promise.resolve({ status: 200, ok: true, json: async () => ({}) });
  });
}

const ADMIN_FORCED = {
  user_id: 1,
  username: 'admin',
  full_name: 'Admin User',
  role: 'ADMIN',
  warehouse_id: 1,
  warehouse_ids: [],
  allowed_functions: [],
  must_change_password: true,
};

const ADMIN_OK = { ...ADMIN_FORCED, must_change_password: false };

function mount(initialPath = '/') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <AuthProvider>
        <WarehouseProvider>
          <App />
        </WarehouseProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
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

describe('ProtectedRoute redirects to /change-password when flag is set', () => {
  it('redirects from / to /change-password', async () => {
    vi.stubGlobal('fetch', mockFetch({ meBody: ADMIN_FORCED }));
    mount('/');
    await waitFor(() => {
      expect(screen.getByText(/First-time setup/i)).toBeInTheDocument();
    });
    expect(
      screen.getByRole('heading', { name: /Change Password/i }),
    ).toBeInTheDocument();
  });

  it('redirects from a deep protected route (/inventory) to /change-password', async () => {
    vi.stubGlobal('fetch', mockFetch({ meBody: ADMIN_FORCED }));
    mount('/inventory');
    await waitFor(() => {
      expect(screen.getByText(/First-time setup/i)).toBeInTheDocument();
    });
  });

  it('redirects from /users to /change-password', async () => {
    vi.stubGlobal('fetch', mockFetch({ meBody: ADMIN_FORCED }));
    mount('/users');
    await waitFor(() => {
      expect(screen.getByText(/First-time setup/i)).toBeInTheDocument();
    });
  });

  it('does not redirect /change-password itself', async () => {
    vi.stubGlobal('fetch', mockFetch({ meBody: ADMIN_FORCED }));
    mount('/change-password');
    await waitFor(() => {
      expect(screen.getByText(/First-time setup/i)).toBeInTheDocument();
    });
  });
});

describe('forced-mode change-password UI', () => {
  it('renders the brand-red banner with the expected copy', async () => {
    vi.stubGlobal('fetch', mockFetch({ meBody: ADMIN_FORCED }));
    mount('/change-password');
    await waitFor(() => {
      const banner = screen.getByRole('alert');
      expect(banner).toBeInTheDocument();
      expect(banner.textContent).toMatch(/First-time setup/i);
      expect(banner.textContent).toMatch(/new admin password/i);
    });
  });

  it('hides the Cancel button in forced mode', async () => {
    vi.stubGlobal('fetch', mockFetch({ meBody: ADMIN_FORCED }));
    mount('/change-password');
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /Change password/i }),
      ).toBeInTheDocument();
    });
    expect(
      screen.queryByRole('button', { name: /^Cancel$/ }),
    ).not.toBeInTheDocument();
  });

  it('hides the sidebar in forced mode', async () => {
    vi.stubGlobal('fetch', mockFetch({ meBody: ADMIN_FORCED }));
    const { container } = mount('/change-password');
    await waitFor(() => {
      expect(screen.getByText(/First-time setup/i)).toBeInTheDocument();
    });
    // No .sidebar-wordmark should render when the sidebar is skipped.
    expect(container.querySelector('.sidebar-wordmark')).toBeNull();
    // .app-layout picks up the forced-change class.
    expect(container.querySelector('.app-layout.forced-change')).toBeTruthy();
  });
});

describe('non-forced flow is unchanged', () => {
  it('renders the normal layout (with sidebar) for a user with flag=false', async () => {
    vi.stubGlobal('fetch', mockFetch({ meBody: ADMIN_OK }));
    const { container } = mount('/');
    await waitFor(() => {
      // Sidebar wordmark is the easy tell that the full shell rendered.
      expect(container.querySelector('.sidebar-wordmark')).toBeTruthy();
    });
    // No forced-change banner.
    expect(screen.queryByText(/First-time setup/i)).toBeNull();
    // No forced-change class on the layout.
    expect(container.querySelector('.app-layout.forced-change')).toBeNull();
  });

  it('lets a flag=false user land on /inventory without redirect', async () => {
    vi.stubGlobal('fetch', mockFetch({ meBody: ADMIN_OK }));
    mount('/inventory');
    // Nothing should redirect them to /change-password.
    await waitFor(() => {
      expect(screen.queryByText(/First-time setup/i)).toBeNull();
    });
  });
});

describe('Cancel button visibility toggles with the flag', () => {
  it('shows Cancel when the user arrives at /change-password voluntarily (flag=false)', async () => {
    vi.stubGlobal('fetch', mockFetch({ meBody: ADMIN_OK }));
    mount('/change-password');
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /Change password/i }),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole('button', { name: /^Cancel$/ }),
    ).toBeInTheDocument();
    // And no banner.
    expect(screen.queryByRole('alert')).toBeNull();
  });
});


// v1.4.2 #98 (Fruxh): /auth/change-password intentionally invalidates
// the server-side session token. The previous flow re-fetched /auth/me
// immediately after, hit 401, left must_change_password=true in the
// client state, and the router guard bounced the operator back to
// /change-password. Fruxh reported this as "First-time setup password
// change returns API error." The fix redirects to /login with a
// success banner instead of refreshing /auth/me with a dead token.
//
// The existing tests in this file always mocked /auth/me as returning
// ADMIN_FORCED on every call; they never exercised the post-change
// 401 case, which is exactly why CI missed this.

function mockPostChangeAuthFlow() {
  let meCalls = 0;
  return vi.fn().mockImplementation((input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const method = (init && init.method) || 'GET';

    if (url.includes('/api/auth/me') && method === 'GET') {
      meCalls += 1;
      // First call: the AuthProvider bootstrap, user is still forced.
      // Subsequent calls: token is dead, backend 401s.
      if (meCalls === 1) {
        return Promise.resolve({
          status: 200,
          ok: true,
          json: async () => ADMIN_FORCED,
        });
      }
      return Promise.resolve({ status: 401, ok: false, json: async () => ({ error: 'unauthorized' }) });
    }

    if (url.includes('/api/auth/change-password') && method === 'POST') {
      return Promise.resolve({
        status: 200,
        ok: true,
        json: async () => ({ message: 'Password changed' }),
      });
    }

    // /api/auth/logout after password change: token is dead, so the
    // real backend responds 401. Best-effort call, result is ignored
    // by the logout() finally block.
    if (url.includes('/api/auth/logout') && method === 'POST') {
      return Promise.resolve({ status: 401, ok: false, json: async () => ({}) });
    }

    return Promise.resolve({ status: 200, ok: true, json: async () => ({}) });
  });
}

describe('post-change redirect (issue #98, Fruxh-reported)', () => {
  it('redirects to /login with a success banner when the token is invalidated', async () => {
    vi.stubGlobal('fetch', mockPostChangeAuthFlow());

    mount('/change-password');

    // Wait for the forced-mode screen to land.
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Change Password/i })).toBeInTheDocument();
    });

    // Fill the three password fields and submit. The labels are not
    // associated via htmlFor, so target by role + order (current, new,
    // confirm).
    const passwordInputs = document.querySelectorAll('input[type="password"]');
    fireEvent.change(passwordInputs[0], { target: { value: 'admin' } });
    fireEvent.change(passwordInputs[1], { target: { value: 'NewPass123' } });
    fireEvent.change(passwordInputs[2], { target: { value: 'NewPass123' } });

    fireEvent.click(screen.getByRole('button', { name: /Change password/i }));

    // Post-fix expected behaviour:
    //   - operator lands on /login
    //   - success banner tells them the password change took and to
    //     sign in again
    // Pre-fix behaviour:
    //   - operator stays on /change-password because the router guard
    //     saw must_change_password=true from the stale context
    //   - no "Please sign in" text
    await waitFor(
      () => {
        expect(screen.getByText(/Please sign in with your new password/i)).toBeInTheDocument();
      },
      { timeout: 3000 },
    );

    // And the forced-mode banner from /change-password is gone.
    expect(screen.queryByText(/First-time setup/i)).toBeNull();
  });
});
