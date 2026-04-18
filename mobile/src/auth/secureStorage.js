/**
 * V-047: auth-credential storage backed by expo-secure-store.
 *
 * Previously the 4 auth keys (jwt_token, user_data, warehouse_id,
 * login_timestamp) lived in AsyncStorage, which is unencrypted SQLite on
 * Android. They are now encrypted by the device keystore. Non-auth keys
 * (api URL, UI preferences, mode flags) remain in AsyncStorage.
 *
 * Callers should use the helpers here rather than importing SecureStore
 * directly so the list of auth keys stays centralized.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import * as SecureStore from 'expo-secure-store';

import { clearAllAuthFromStores } from './authStorageClear';
import { migrateAsyncStorageToSecureStore } from './authStorageMigration';

export const AUTH_STORAGE_KEYS = [
  'jwt_token',
  'user_data',
  'warehouse_id',
  'login_timestamp',
];

export async function getAuthItem(key) {
  return SecureStore.getItemAsync(key);
}

export async function setAuthItem(key, value) {
  return SecureStore.setItemAsync(key, value);
}

export async function deleteAuthItem(key) {
  try {
    await SecureStore.deleteItemAsync(key);
  } catch {
    // deleteItemAsync throws if the key is not set; callers treat delete as idempotent.
  }
}

export async function clearAllAuth() {
  // V-104: clearAllAuthFromStores (in authStorageClear.js) is the pure
  // form suitable for unit tests with in-memory mocks. Here we bind it
  // to the real AsyncStorage + SecureStore backends.
  return clearAllAuthFromStores(AUTH_STORAGE_KEYS, AsyncStorage, SecureStore);
}

export async function runAuthStorageMigration() {
  return migrateAsyncStorageToSecureStore(AUTH_STORAGE_KEYS, AsyncStorage, SecureStore);
}
