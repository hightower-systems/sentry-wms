/**
 * V-020: safe client-side error logging.
 *
 * React's ErrorBoundary default is to `console.error` the whole error
 * object plus componentStack. On a shared workstation or with a malicious
 * browser extension, that output can leak Authorization headers, JWT
 * strings embedded in fetch error messages, or URL userinfo.
 *
 * Two layers of defence:
 *   1. In production builds, log only the error name + a scrubbed message.
 *      No component stack, no object dump.
 *   2. In dev builds, keep the full output for debugging.
 *   3. In both modes, pre-scrub any string we emit so URL userinfo and
 *      Bearer-token substrings never reach the console.
 */

const BEARER_RE = /Bearer\s+[A-Za-z0-9._~+/=-]+/g;
const URL_USERINFO_RE = /(https?:\/\/)[^@/\s"'`]+:[^@/\s"'`]+@/g;
// Conservative JWT-shape matcher: three base64url segments joined by dots.
const JWT_RE = /\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g;

export function scrubLogString(input) {
  if (input === null || input === undefined) return '';
  let s = typeof input === 'string' ? input : String(input);
  s = s.replace(BEARER_RE, 'Bearer REDACTED');
  s = s.replace(URL_USERINFO_RE, '$1');
  s = s.replace(JWT_RE, 'REDACTED_JWT');
  return s;
}

export function logBoundaryError(error, errorInfo) {
  const isProd = typeof import.meta !== 'undefined' && import.meta?.env?.PROD;
  if (isProd) {
    const name = (error && error.name) || 'Error';
    const message = scrubLogString(error && error.message);
    // eslint-disable-next-line no-console
    console.error(`[ErrorBoundary] ${name}: ${message}`);
    return;
  }
  const safeMessage = scrubLogString(error && error.message);
  const safeStack = scrubLogString(errorInfo && errorInfo.componentStack);
  // eslint-disable-next-line no-console
  console.error('[ErrorBoundary]', safeMessage, safeStack);
}
