import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api.js';

function response(status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    clone: () => response(status),
    json: () => Promise.resolve({}),
  };
}

describe('API form uploads', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.cookie = 'sentry_csrf=; Max-Age=0; path=/';
  });

  it('lets the browser set the multipart boundary and keeps the CSRF header', async () => {
    document.cookie = 'sentry_csrf=csrf-test; path=/';
    const fetchMock = vi.fn(() => Promise.resolve(response(201)));
    vi.stubGlobal('fetch', fetchMock);
    const form = new FormData();
    form.append('warehouse_id', '1');

    await api.postForm('/shipyard/test', form);

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/shipyard/test');
    expect(options.method).toBe('POST');
    expect(options.body).toBe(form);
    expect(options.headers['Content-Type']).toBeUndefined();
    expect(options.headers['X-CSRF-Token']).toBe('csrf-test');
  });

  it('keeps JSON content type for JSON writes', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(response(200)));
    vi.stubGlobal('fetch', fetchMock);

    await api.post('/shipyard/test', { ok: true });

    const [, options] = fetchMock.mock.calls[0];
    expect(options.headers['Content-Type']).toBe('application/json');
    expect(options.body).toBe('{"ok":true}');
  });
});
