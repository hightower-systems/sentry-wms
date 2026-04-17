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
 *   - Always remove the AsyncStorage entry after a successful copy so the
 *     plaintext value does not linger.
 *   - Errors on individual keys are swallowed: a best-effort migration must
 *     not brick an existing session if e.g. the keystore is unavailable on
 *     a particular device.
 *   - Idempotent: a second run after a successful migration is a no-op.
 *
 * Returns the list of keys that were actually migrated this call (for
 * logging / testing).
 */
export async function migrateAsyncStorageToSecureStore(keys, asyncStore, secureStore) {
  const migrated = [];
  for (const key of keys) {
    try {
      const legacyValue = await asyncStore.getItem(key);
      if (legacyValue === null || legacyValue === undefined) {
        continue;
      }
      const existing = await secureStore.getItemAsync(key);
      if (existing === null || existing === undefined) {
        await secureStore.setItemAsync(key, legacyValue);
      }
      await asyncStore.removeItem(key);
      migrated.push(key);
    } catch {
      // Swallow per-key errors so one bad key does not abort the whole migration.
    }
  }
  return migrated;
}
