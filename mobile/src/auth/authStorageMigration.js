/**
 * V-047: migration from AsyncStorage to SecureStore for auth credentials.
 *
 * Pure function - takes storage interfaces as arguments so it can be unit
 * tested without pulling in React Native runtime modules. Used at app
 * bootstrap before any auth key is read.
 *
 * Behavior:
 *   - For each key: read from AsyncStorage. If present, copy to SecureStore
 *     (unless SecureStore already has a non-null value, in which case we
 *     trust SecureStore and just clear the AsyncStorage copy).
 *   - Always remove the AsyncStorage entry in a finally block so the
 *     plaintext value never survives a failed SecureStore write (V-104).
 *     A keystore-unavailable device logs the user out on next launch,
 *     which is the correct failure mode: the alternative is a plaintext
 *     token lingering in AsyncStorage indefinitely.
 *   - Per-key errors are swallowed so one bad key does not abort the
 *     whole migration. The AsyncStorage entry is still removed via the
 *     finally path, so credential residue never survives.
 *   - Idempotent: a second run after a successful migration is a no-op.
 *
 * Returns the list of keys that were actually migrated this call (for
 * logging / testing).
 */
export async function migrateAsyncStorageToSecureStore(keys, asyncStore, secureStore) {
  const migrated = [];
  for (const key of keys) {
    let hadLegacy = false;
    try {
      const legacyValue = await asyncStore.getItem(key);
      if (legacyValue === null || legacyValue === undefined) {
        continue;
      }
      hadLegacy = true;
      const existing = await secureStore.getItemAsync(key);
      if (existing === null || existing === undefined) {
        await secureStore.setItemAsync(key, legacyValue);
      }
      migrated.push(key);
    } catch {
      // V-104: SecureStore write failed (keystore unavailable, disk
      // full, etc.). Fall through to the finally block so the
      // plaintext AsyncStorage entry is cleared. The user will be
      // logged out on next read; the alternative was a persistent
      // plaintext token on devices where migration silently failed.
    } finally {
      if (hadLegacy) {
        try {
          await asyncStore.removeItem(key);
        } catch {
          // AsyncStorage.removeItem almost never throws; if it does,
          // another migration run (or clearAllAuth) will retry.
        }
      }
    }
  }
  return migrated;
}
