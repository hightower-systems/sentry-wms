/**
 * V-021: map backend error responses to user-friendly strings.
 *
 * The admin SPA used to render `data.error` verbatim, which surfaced
 * backend internals (SQL constraint names, Python exception reprs) to
 * end users. This helper converts a response payload to a finite,
 * human-readable message. Anything not explicitly mapped falls back to
 * a generic message and never echoes the raw backend string.
 */

// Backend-defined `error` values that are safe to surface as-is because
// they are end-user oriented (not internal diagnostics). Keys map to
// the user-facing string the UI should show.
const KNOWN_ERROR_MESSAGES = {
  validation_error: 'One or more fields have invalid values.',
  unsupported_media_type: 'That request format is not supported.',
  'Invalid username or password': 'Wrong username or password.',
  'Account disabled or deleted': 'Your account is no longer active. Contact an admin.',
  'Token expired': 'Your session has expired. Please sign in again.',
  Unauthorized: 'You need to sign in to continue.',
  Forbidden: 'You do not have permission for that action.',
  'CSRF token missing or invalid': 'Your session is out of sync. Refresh the page and try again.',
  'Access denied for this warehouse': 'You do not have access to that warehouse.',
  'Current password is incorrect': 'Current password is incorrect.',
  'User not found': 'Account not found.',
};

export function friendlyError(payload, fallback = 'Something went wrong. Please try again.') {
  if (!payload || typeof payload !== 'object') return fallback;
  const code = payload.error;
  if (code && Object.prototype.hasOwnProperty.call(KNOWN_ERROR_MESSAGES, code)) {
    return KNOWN_ERROR_MESSAGES[code];
  }
  return fallback;
}

/**
 * Resolve a friendly error from a fetch Response. Reads the JSON body
 * (if any) and maps via friendlyError. Use this when you have a non-ok
 * Response and want the right user-facing string.
 */
export async function friendlyErrorFromResponse(res, fallback) {
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  return friendlyError(body, fallback);
}
