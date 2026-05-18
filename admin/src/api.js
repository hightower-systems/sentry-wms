const API_BASE = '/api';

function getCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)sentry_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

async function apiFetch(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const needsCsrf = method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS';
  const csrfToken = needsCsrf ? getCsrfToken() : null;
  // avid-overhaul-mk1 P6.1: callers can pass silentPermissionDenied:true
  // to opt OUT of the global Permissions Error popup for background
  // fetches (sidebar badges, navshell settings probes). Without it
  // every USER landing on Dashboard would see the modal fire on
  // page mount before they did anything explicit.
  const { silentPermissionDenied = false, ...fetchOptions } = options;
  const headers = {
    'Content-Type': 'application/json',
    ...(csrfToken && { 'X-CSRF-Token': csrfToken }),
    ...fetchOptions.headers,
  };
  const res = await fetch(`${API_BASE}${path}`, {
    ...fetchOptions,
    headers,
    credentials: 'include',
  });
  if (res.status === 401 && !path.startsWith('/auth/login') && !path.startsWith('/auth/me')) {
    window.location.href = '/login';
    return;
  }
  // P6.1: surface page-permission rejections via a window event so
  // Layout can render a "Permissions Error" modal wherever the user
  // is. Response shape comes from @require_admin_or_page_permission
  // ({error: "Permission denied", page_key: "..."}). Peek at the body
  // without consuming the stream (clone first) so the caller can
  // still .json() the response.
  if (res.status === 403 && !silentPermissionDenied) {
    try {
      const peek = await res.clone().json();
      if (peek?.error === 'Permission denied' && peek?.page_key) {
        window.dispatchEvent(new CustomEvent('sentry:permission-denied', {
          detail: { page_key: peek.page_key, path },
        }));
      }
    } catch (_) { /* non-JSON 403 (legacy / unrelated) */ }
  }
  return res;
}

export const api = {
  get: (path, opts) => apiFetch(path, { ...(opts || {}) }),
  post: (path, body, opts) => apiFetch(path, { method: 'POST', body: JSON.stringify(body), ...(opts || {}) }),
  put: (path, body, opts) => apiFetch(path, { method: 'PUT', body: JSON.stringify(body), ...(opts || {}) }),
  patch: (path, body, opts) => apiFetch(path, { method: 'PATCH', body: JSON.stringify(body), ...(opts || {}) }),
  delete: (path, opts) => apiFetch(path, { method: 'DELETE', ...(opts || {}) }),
};
