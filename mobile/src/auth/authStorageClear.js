/**
 * V-104: pure implementation of clearAllAuth.
 *
 * Accepts storage interfaces so it can be unit tested with in-memory
 * mocks (the same pattern as authStorageMigration.js). Wipes every
 * provided key from BOTH SecureStore and AsyncStorage, because a
 * failed migration may have left a plaintext copy in AsyncStorage
 * that SecureStore deletes would miss.
 *
 * Kept in its own file so tests can import it without dragging in the
 * react-native async-storage and expo-secure-store modules (which
 * vitest cannot parse out of the box).
 */
export async function clearAllAuthFromStores(keys, asyncStore, secureStore) {
  await Promise.all(
    keys.map(async (k) => {
      try {
        await secureStore.deleteItemAsync(k);
      } catch {
        // deleteItemAsync throws if the key is not set; idempotent.
      }
    }),
  );
  await Promise.all(
    keys.map(async (k) => {
      try {
        await asyncStore.removeItem(k);
      } catch {
        // AsyncStorage.removeItem almost never throws; a failure here
        // does not block the SecureStore deletes above.
      }
    }),
  );
}
