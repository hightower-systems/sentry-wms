import AsyncStorage from '@react-native-async-storage/async-storage';

// Build-time default from .env / eas.json env
const DEFAULT_API_URL = process.env.EXPO_PUBLIC_API_URL || 'http://localhost:5000';
const API_URL_KEY = 'sentry_api_url';

// Runtime-configurable API URL (cached in memory after first load)
let _cachedApiUrl = null;

async function getApiUrl() {
  if (_cachedApiUrl) return _cachedApiUrl;
  const stored = await AsyncStorage.getItem(API_URL_KEY).catch(() => null);
  _cachedApiUrl = stored || DEFAULT_API_URL;
  return _cachedApiUrl;
}

/**
 * Set API URL at runtime (from settings screen).
 * Takes effect immediately — no app restart needed.
 */
export async function setApiUrl(url) {
  const trimmed = url.replace(/\/+$/, '').trim();
  _cachedApiUrl = trimmed;
  await AsyncStorage.setItem(API_URL_KEY, trimmed).catch(() => {});
}

/** Get the current API URL (for display in settings). */
export async function getStoredApiUrl() {
  return (await AsyncStorage.getItem(API_URL_KEY).catch(() => null)) || DEFAULT_API_URL;
}

let logoutHandler = null;
export const setLogoutHandler = (handler) => {
  logoutHandler = handler;
};

async function request(method, path, body) {
  const token = await AsyncStorage.getItem('jwt_token');
  const headers = { 'Content-Type': 'application/json' };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const options = { method, headers };
  if (body && method !== 'GET') {
    options.body = JSON.stringify(body);
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  options.signal = controller.signal;

  const apiUrl = await getApiUrl();
  const fullUrl = `${apiUrl}${path}`;
  console.log(`[API_DEBUG] ${method} ${path}`, body ? JSON.stringify(body).slice(0, 200) : '');

  let response;
  try {
    response = await fetch(fullUrl, options);
  } catch (err) {
    clearTimeout(timeout);
    console.log(`[API_DEBUG] ${method} ${path} NETWORK ERROR:`, err.message);
    if (err.name === 'AbortError') {
      const timeoutErr = new Error('Request timeout');
      timeoutErr.response = null;
      throw timeoutErr;
    }
    throw err;
  }
  clearTimeout(timeout);

  let data = null;
  const text = await response.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  console.log(`[API_DEBUG] ${method} ${path} → ${response.status}`, JSON.stringify(data).slice(0, 300));

  if (response.status === 401 && logoutHandler) {
    await logoutHandler();
  }

  if (!response.ok) {
    const error = new Error(data?.error || `HTTP ${response.status}`);
    error.response = { status: response.status, data };
    throw error;
  }

  return { data, status: response.status };
}

const client = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body),
  put: (path, body) => request('PUT', path, body),
  delete: (path) => request('DELETE', path),
};

export default client;
