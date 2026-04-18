/**
 * V-020: tests for the client-side log scrubber.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { scrubLogString, logBoundaryError } from '../utils/safeLogging.js';

describe('scrubLogString', () => {
  it('redacts Bearer tokens', () => {
    const out = scrubLogString('Request failed with header Authorization: Bearer abc.def.ghi');
    expect(out).toContain('Bearer REDACTED');
    expect(out).not.toContain('abc.def.ghi');
  });

  it('strips URL userinfo', () => {
    const out = scrubLogString('fetch https://alice:s3cret@api.example.com/orders failed');
    expect(out).not.toContain('alice');
    expect(out).not.toContain('s3cret');
    expect(out).toContain('https://api.example.com/orders');
  });

  it('redacts JWT-shaped tokens', () => {
    const jwt = 'eyJabcdefghij.eyJpayloadabc.signaturesig123';
    const out = scrubLogString(`got token ${jwt} in response`);
    expect(out).not.toContain(jwt);
    expect(out).toContain('REDACTED_JWT');
  });

  it('returns empty string for null/undefined', () => {
    expect(scrubLogString(null)).toBe('');
    expect(scrubLogString(undefined)).toBe('');
  });

  it('coerces non-strings', () => {
    expect(scrubLogString(new Error('plain message'))).toContain('plain message');
  });

  it('leaves safe strings unchanged', () => {
    expect(scrubLogString('just a plain error')).toBe('just a plain error');
  });
});

describe('logBoundaryError', () => {
  let errSpy;
  beforeEach(() => {
    errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => {
    errSpy.mockRestore();
  });

  it('scrubs Bearer tokens from the logged message', () => {
    const err = new Error('Request Authorization: Bearer abc.def.ghi failed');
    logBoundaryError(err, { componentStack: '' });
    const loggedArgs = errSpy.mock.calls[0].map((a) => String(a)).join(' ');
    expect(loggedArgs).not.toContain('abc.def.ghi');
    expect(loggedArgs).toContain('Bearer REDACTED');
  });

  it('scrubs URL userinfo from componentStack in dev mode', () => {
    const err = new Error('boom');
    logBoundaryError(err, { componentStack: 'at https://u:p@host/path' });
    const loggedArgs = errSpy.mock.calls[0].map((a) => String(a)).join(' ');
    expect(loggedArgs).not.toContain('u:p');
  });
});
