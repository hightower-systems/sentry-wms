/**
 * V-021: tests for the friendly-error helper.
 */

import { describe, it, expect } from 'vitest';
import { friendlyError } from '../utils/friendlyError.js';

describe('friendlyError', () => {
  it('maps known error codes to user-friendly strings', () => {
    expect(friendlyError({ error: 'validation_error' })).toBe(
      'One or more fields have invalid values.'
    );
    expect(friendlyError({ error: 'Invalid username or password' })).toBe(
      'Wrong username or password.'
    );
    expect(friendlyError({ error: 'CSRF token missing or invalid' })).toBe(
      'Your session is out of sync. Refresh the page and try again.'
    );
  });

  it('returns fallback for unknown error codes', () => {
    expect(friendlyError({ error: 'some_internal_code' })).toBe(
      'Something went wrong. Please try again.'
    );
  });

  it('never echoes raw backend error strings', () => {
    const leakyPayload = {
      error: 'UNIQUE_CONSTRAINT_VIOLATION on users_username_key',
      stack: 'Traceback: /app/routes/admin_users.py line 42',
    };
    const result = friendlyError(leakyPayload);
    expect(result).not.toContain('UNIQUE_CONSTRAINT_VIOLATION');
    expect(result).not.toContain('Traceback');
    expect(result).not.toContain('admin_users.py');
  });

  it('uses the custom fallback when provided', () => {
    expect(friendlyError({ error: 'mystery' }, 'Could not save item.')).toBe(
      'Could not save item.'
    );
  });

  it('handles null/non-object payloads safely', () => {
    expect(friendlyError(null)).toBe('Something went wrong. Please try again.');
    expect(friendlyError(undefined)).toBe('Something went wrong. Please try again.');
    expect(friendlyError('some string')).toBe('Something went wrong. Please try again.');
  });

  it('handles payload with no error field', () => {
    expect(friendlyError({})).toBe('Something went wrong. Please try again.');
  });
});
