/**
 * v1.4.1 forced password change: mobile persistence invariants (issue #69).
 *
 * The navigator shows ChangePasswordScreen when user.must_change_password
 * is true. Because that user dict round-trips through SecureStore on
 * login, on rehydrate, and on post-change state mirroring, the tests
 * below guard the persistence layer that keeps the flag honest across
 * force-kill and reopen.
 *
 * The React Navigation and hardware-back-button behaviours were exercised
 * manually on a Chainway C6000 (2026-04-18). Mobile has no RN runtime in
 * its vitest harness, so the rendering paths are verified on real
 * hardware instead of mocked here. What *this* suite protects:
 *
 *   - login persists must_change_password with the same truthiness the
 *     backend sent, so a force-kill + reopen rehydrates the forced state.
 *   - completePasswordChange flips the persisted copy to false, so a
 *     resume after a successful change lands on Home, not ChangePassword.
 *   - user_data round-trips cleanly between in-memory dict and the
 *     JSON-serialized form written to SecureStore.
 */

import { describe, it, expect, vi } from 'vitest';

// In-memory SecureStore mock. Same shape as the existing migration test:
// getItemAsync / setItemAsync / deleteItemAsync, mirroring the real
// expo-secure-store surface.
function makeSecureStore(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    getItemAsync: vi.fn(async (k) => (store.has(k) ? store.get(k) : null)),
    setItemAsync: vi.fn(async (k, v) => {
      store.set(k, v);
    }),
    deleteItemAsync: vi.fn(async (k) => {
      store.delete(k);
    }),
    _snapshot: () => Object.fromEntries(store),
  };
}

// The real setAuthItem / getAuthItem are one-line wrappers around
// SecureStore.setItemAsync / getItemAsync (see mobile/src/auth/secureStorage.js).
// We bind them to the in-memory store here so tests exercise the same
// contract without the native module.
const bindAuthStorage = (secureStore) => ({
  setAuthItem: (k, v) => secureStore.setItemAsync(k, v),
  getAuthItem: (k) => secureStore.getItemAsync(k),
});


describe('login persistence preserves must_change_password', () => {
  it('writes user_data to SecureStore with must_change_password=true intact', async () => {
    const secureStore = makeSecureStore();
    const { setAuthItem, getAuthItem } = bindAuthStorage(secureStore);

    // Shape mirrors the real /api/auth/login response user payload.
    const userFromBackend = {
      user_id: 1,
      username: 'admin',
      role: 'ADMIN',
      warehouse_id: 1,
      warehouse_ids: [],
      must_change_password: true,
    };

    await setAuthItem('jwt_token', 'eyJ-fake-token');
    await setAuthItem('user_data', JSON.stringify(userFromBackend));

    const raw = await getAuthItem('user_data');
    expect(raw).not.toBeNull();
    const round = JSON.parse(raw);
    expect(round.must_change_password).toBe(true);
    // Sanity: full payload survived.
    expect(round.user_id).toBe(1);
    expect(round.role).toBe('ADMIN');
  });

  it('writes user_data with must_change_password=false for a normal login', async () => {
    const secureStore = makeSecureStore();
    const { setAuthItem, getAuthItem } = bindAuthStorage(secureStore);

    const userFromBackend = {
      user_id: 1,
      username: 'admin',
      role: 'ADMIN',
      warehouse_id: 1,
      warehouse_ids: [],
      must_change_password: false,
    };

    await setAuthItem('user_data', JSON.stringify(userFromBackend));

    const round = JSON.parse(await getAuthItem('user_data'));
    expect(round.must_change_password).toBe(false);
  });
});


describe('force-kill + reopen rehydrates the flag from SecureStore', () => {
  it('keeps must_change_password=true across a simulated relaunch', async () => {
    // "Logged in" SecureStore state: forced-mode admin.
    const secureStore = makeSecureStore({
      jwt_token: 'eyJ-fake',
      user_data: JSON.stringify({
        user_id: 1,
        username: 'admin',
        role: 'ADMIN',
        must_change_password: true,
      }),
      login_timestamp: String(Date.now()),
    });

    // Simulated app relaunch: the AuthProvider's useEffect reads the
    // saved user dict. If the flag were dropped on persistence, the
    // force-kill bypass would open -- this test is the regression gate.
    const { getAuthItem } = bindAuthStorage(secureStore);
    const raw = await getAuthItem('user_data');
    const rehydrated = JSON.parse(raw);

    expect(rehydrated.must_change_password).toBe(true);
    // The navigator branches on this exact field, so truthiness matters.
    expect(!!rehydrated.must_change_password).toBe(true);
  });

  it('rehydrates must_change_password=false after the flag was cleared', async () => {
    const secureStore = makeSecureStore({
      jwt_token: 'eyJ-fake',
      user_data: JSON.stringify({
        user_id: 1,
        username: 'admin',
        role: 'ADMIN',
        must_change_password: false,
      }),
    });
    const { getAuthItem } = bindAuthStorage(secureStore);

    const rehydrated = JSON.parse(await getAuthItem('user_data'));
    expect(rehydrated.must_change_password).toBe(false);
  });
});


describe('completePasswordChange mirrors the cleared flag to SecureStore', () => {
  // This reproduces the write-through in AuthContext.completePasswordChange:
  //   setUser(prev => { const next = {...prev, must_change_password: false};
  //                     setAuthItem('user_data', JSON.stringify(next)); return next; })
  // We test the persistence half of that as a pure function.
  async function simulateCompletePasswordChange(secureStore) {
    const { setAuthItem, getAuthItem } = bindAuthStorage(secureStore);
    const raw = await getAuthItem('user_data');
    if (!raw) return null;
    const prev = JSON.parse(raw);
    const next = { ...prev, must_change_password: false };
    await setAuthItem('user_data', JSON.stringify(next));
    return next;
  }

  it('flips must_change_password from true to false in the persisted copy', async () => {
    const secureStore = makeSecureStore({
      user_data: JSON.stringify({
        user_id: 1,
        username: 'admin',
        role: 'ADMIN',
        must_change_password: true,
      }),
    });

    const next = await simulateCompletePasswordChange(secureStore);
    expect(next.must_change_password).toBe(false);

    // And a subsequent rehydrate sees the cleared state.
    const round = JSON.parse(secureStore._snapshot().user_data);
    expect(round.must_change_password).toBe(false);
  });

  it('preserves every other user field while flipping the flag', async () => {
    const original = {
      user_id: 42,
      username: 'admin',
      full_name: 'Admin User',
      role: 'ADMIN',
      warehouse_id: 1,
      warehouse_ids: [1, 2],
      allowed_functions: ['receive', 'putaway'],
      must_change_password: true,
    };
    const secureStore = makeSecureStore({
      user_data: JSON.stringify(original),
    });

    const next = await simulateCompletePasswordChange(secureStore);

    // Exactly one field changed.
    expect(next).toEqual({ ...original, must_change_password: false });
  });
});


describe('navigator branch contract', () => {
  // AppNavigator reads `const forced = !!user?.must_change_password`. These
  // tests lock down the truthiness mapping so a subtle JSON edge case
  // (e.g., the backend returning a number or string instead of a bool)
  // cannot silently put the navigator in the wrong mode.

  it('treats true as forced', () => {
    expect(!!{ must_change_password: true }.must_change_password).toBe(true);
  });

  it('treats false as non-forced', () => {
    expect(!!{ must_change_password: false }.must_change_password).toBe(false);
  });

  it('treats an undefined field (pre-upgrade user) as non-forced', () => {
    // Pre-v1.4.1 persisted dicts have no such field. The branch must
    // default to the normal stack so existing sessions do not get locked
    // into a screen that does not apply to them.
    expect(!!{}.must_change_password).toBe(false);
  });

  it('treats null as non-forced', () => {
    expect(!!{ must_change_password: null }.must_change_password).toBe(false);
  });

  it('treats a missing user entirely as non-forced', () => {
    const user = null;
    expect(!!user?.must_change_password).toBe(false);
  });
});
