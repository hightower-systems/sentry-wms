/**
 * v1.4.2 #100: hook is now beforeunload-only (the useBlocker variant
 * from #94 required a data router the admin does not run under and
 * crashed Settings on mount). These tests verify the surviving
 * contract:
 *
 *   - the hook attaches a beforeunload listener on mount
 *   - the listener calls preventDefault + sets returnValue when
 *     isDirty is true
 *   - the listener is a no-op when isDirty is false
 *   - the listener is removed on unmount (no stale leak)
 *
 * No router mocks are needed -- the hook no longer imports
 * react-router-dom.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';

import { useDirtyFormGuard } from '../hooks/useDirtyFormGuard.js';

function TestComponent({ isDirty }) {
  useDirtyFormGuard(isDirty);
  return null;
}


describe('useDirtyFormGuard', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('mounts without error and attaches a beforeunload listener', () => {
    const addSpy = vi.spyOn(window, 'addEventListener');
    render(<TestComponent isDirty={false} />);
    expect(addSpy).toHaveBeenCalledWith('beforeunload', expect.any(Function));
  });

  it('the beforeunload handler is a no-op when isDirty is false', () => {
    const handlers = [];
    vi.spyOn(window, 'addEventListener').mockImplementation((ev, fn) => {
      if (ev === 'beforeunload') handlers.push(fn);
    });
    render(<TestComponent isDirty={false} />);

    const event = { preventDefault: vi.fn(), returnValue: undefined };
    handlers[0](event);
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(event.returnValue).toBeUndefined();
  });

  it('prevents default and sets returnValue when isDirty is true', () => {
    const handlers = [];
    vi.spyOn(window, 'addEventListener').mockImplementation((ev, fn) => {
      if (ev === 'beforeunload') handlers.push(fn);
    });
    render(<TestComponent isDirty />);

    const event = { preventDefault: vi.fn(), returnValue: undefined };
    handlers[0](event);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(event.returnValue).toBe('');
  });

  it('removes the listener on unmount (no stale leak)', () => {
    const removeSpy = vi.spyOn(window, 'removeEventListener');
    const { unmount } = render(<TestComponent isDirty />);
    unmount();
    expect(removeSpy).toHaveBeenCalledWith('beforeunload', expect.any(Function));
  });
});
