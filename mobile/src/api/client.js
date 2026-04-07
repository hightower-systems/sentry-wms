import AsyncStorage from '@react-native-async-storage/async-storage';

const API_URL = 'http://10.0.0.155:5000';

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

  let response;
  try {
    response = await fetch(`${API_URL}${path}`, options);
  } catch (err) {
    clearTimeout(timeout);
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
