/**
 * Tests for V-047 AsyncStorage -> SecureStore migration.
 *
 * The migration function accepts storage interfaces so we can test with
 * in-memory mocks. No React Native runtime needed.
 */

import { describe, it, expect, vi } from 'vitest';

import { migrateAsyncStorageToSecureStore } from '../authStorageMigration';

const AUTH_KEYS = ['jwt_token', 'user_data', 'warehouse_id', 'login_timestamp'];

function makeAsyncStore(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    getItem: vi.fn(async (k) => (store.has(k) ? store.get(k) : null)),
    setItem: vi.fn(async (k, v) => { store.set(k, v); }),
    removeItem: vi.fn(async (k) => { store.delete(k); }),
    _snapshot: () => Object.fromEntries(store),
  };
}

function makeSecureStore(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    getItemAsync: vi.fn(async (k) => (store.has(k) ? store.get(k) : null)),
    setItemAsync: vi.fn(async (k, v) => { store.set(k, v); }),
    deleteItemAsync: vi.fn(async (k) => { store.delete(k); }),
    _snapshot: () => Object.fromEntries(store),
  };
}

describe('migrateAsyncStorageToSecureStore', () => {
  it('moves every auth key from AsyncStorage into SecureStore', async () => {
    const asyncStore = makeAsyncStore({
      jwt_token: 'token-abc',
      user_data: '{"username":"admin"}',
      warehouse_id: '1',
      login_timestamp: '1700000000000',
    });
    const secureStore = makeSecureStore();

    const migrated = await migrateAsyncStorageToSecureStore(
      AUTH_KEYS, asyncStore, secureStore,
    );

    expect(migrated.sort()).toEqual([...AUTH_KEYS].sort());
    expect(secureStore._snapshot()).toEqual({
      jwt_token: 'token-abc',
      user_data: '{"username":"admin"}',
      warehouse_id: '1',
      login_timestamp: '1700000000000',
    });
  });

  it('clears the AsyncStorage copy after a successful migration', async () => {
    const asyncStore = makeAsyncStore({
      jwt_token: 'token-abc',
      user_data: '{"x":1}',
    });
    const secureStore = makeSecureStore();

    await migrateAsyncStorageToSecureStore(AUTH_KEYS, asyncStore, secureStore);

    // The plaintext values must not linger in AsyncStorage.
    expect(asyncStore._snapshot()).toEqual({});
    expect(asyncStore.removeItem).toHaveBeenCalledWith('jwt_token');
    expect(asyncStore.removeItem).toHaveBeenCalledWith('user_data');
  });

  it('is idempotent: a second run finds nothing to migrate', async () => {
    const asyncStore = makeAsyncStore({ jwt_token: 'abc' });
    const secureStore = makeSecureStore();

    const first = await migrateAsyncStorageToSecureStore(AUTH_KEYS, asyncStore, secureStore);
    const second = await migrateAsyncStorageToSecureStore(AUTH_KEYS, asyncStore, secureStore);

    expect(first).toEqual(['jwt_token']);
    expect(second).toEqual([]);
    expect(secureStore._snapshot()).toEqual({ jwt_token: 'abc' });
  });

  it('skips keys that are not present in AsyncStorage', async () => {
    const asyncStore = makeAsyncStore({ jwt_token: 'only-this' });
    const secureStore = makeSecureStore();

    const migrated = await migrateAsyncStorageToSecureStore(AUTH_KEYS, asyncStore, secureStore);

    expect(migrated).toEqual(['jwt_token']);
    expect(secureStore.setItemAsync).toHaveBeenCalledTimes(1);
  });

  it('does not overwrite an existing SecureStore value', async () => {
    // Someone who already migrated and then logged in again: SecureStore
    // holds the fresh token; AsyncStorage should still get cleared but
    // the newer SecureStore value must win.
    const asyncStore = makeAsyncStore({ jwt_token: 'stale-plaintext' });
    const secureStore = makeSecureStore({ jwt_token: 'fresh-encrypted' });

    await migrateAsyncStorageToSecureStore(AUTH_KEYS, asyncStore, secureStore);

    expect(secureStore._snapshot().jwt_token).toBe('fresh-encrypted');
    expect(asyncStore._snapshot()).toEqual({});
  });

  it('continues migrating remaining keys if one key throws', async () => {
    const asyncStore = makeAsyncStore({
      jwt_token: 'a',
      user_data: 'b',
      warehouse_id: 'c',
      login_timestamp: 'd',
    });
    const secureStore = makeSecureStore();
    secureStore.setItemAsync.mockImplementationOnce(async () => {
      throw new Error('keystore unavailable');
    });

    const migrated = await migrateAsyncStorageToSecureStore(AUTH_KEYS, asyncStore, secureStore);

    // jwt_token fails; the other three still migrate.
    expect(migrated).not.toContain('jwt_token');
    expect(migrated.sort()).toEqual(['login_timestamp', 'user_data', 'warehouse_id']);
    // Failed key stays in AsyncStorage so the user is not logged out.
    expect(asyncStore._snapshot()).toEqual({ jwt_token: 'a' });
  });

  it('leaves non-auth AsyncStorage keys untouched', async () => {
    const asyncStore = makeAsyncStore({
      jwt_token: 'x',
      sentry_api_url: 'http://example',
      sentry_last_username: 'admin',
      sentry_scan_mode: 'single',
    });
    const secureStore = makeSecureStore();

    await migrateAsyncStorageToSecureStore(AUTH_KEYS, asyncStore, secureStore);

    expect(asyncStore._snapshot()).toEqual({
      sentry_api_url: 'http://example',
      sentry_last_username: 'admin',
      sentry_scan_mode: 'single',
    });
    expect(secureStore._snapshot()).toEqual({ jwt_token: 'x' });
  });

  it('treats undefined from getItem the same as null', async () => {
    const asyncStore = {
      getItem: vi.fn(async () => undefined),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    };
    const secureStore = makeSecureStore();

    const migrated = await migrateAsyncStorageToSecureStore(AUTH_KEYS, asyncStore, secureStore);

    expect(migrated).toEqual([]);
    expect(secureStore.setItemAsync).not.toHaveBeenCalled();
    expect(asyncStore.removeItem).not.toHaveBeenCalled();
  });
});
