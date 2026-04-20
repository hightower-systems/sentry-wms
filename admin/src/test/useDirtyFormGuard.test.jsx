/**
 * v1.4.2 #94: smoke test for the unsaved-changes hook.
 *
 * A full simulation of navigation interception would need a
 * MemoryRouter with concurrent routes + a click event on a Link; that
 * apparatus is more weight than the hook is worth. What this file
 * locks down is the shape:
 *
 *   - Hook imports and runs without error inside a router context
 *     (it depends on react-router's `useBlocker`).
 *   - When isDirty = true and the blocker reports a blocked state,
 *     the hook calls window.confirm with the supplied message.
 *   - confirm() returning true -> blocker.proceed() fires.
 *   - confirm() returning false -> blocker.reset() fires.
 *
 * The hook is the only layer of logic on top of useBlocker, so
 * verifying those four conditions is enough.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

// Mock useBlocker: we drive the returned blocker object from the tests
// rather than triggering actual router navigation.
const blockerState = { current: { state: 'unblocked', proceed: vi.fn(), reset: vi.fn() } };
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useBlocker: (shouldBlock) => {
      // Match the real hook's signature: called with a function that the
      // hook will invoke when navigation happens. We do not re-invoke it
      // here; the blocker object we return is driven by blockerState.
      blockerState.lastShouldBlock = shouldBlock;
      return blockerState.current;
    },
  };
});

import { useDirtyFormGuard } from '../hooks/useDirtyFormGuard.js';

function TestComponent({ isDirty }) {
  useDirtyFormGuard(isDirty, 'TEST MESSAGE');
  return null;
}

function renderWithRouter(ui) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}


describe('useDirtyFormGuard', () => {
  beforeEach(() => {
    blockerState.current = { state: 'unblocked', proceed: vi.fn(), reset: vi.fn() };
    vi.restoreAllMocks();
  });

  it('mounts without error inside a router context', () => {
    expect(() => renderWithRouter(<TestComponent isDirty={false} />)).not.toThrow();
  });

  it('does not call confirm when the blocker is not blocked', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithRouter(<TestComponent isDirty />);
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('calls confirm with the supplied message when the blocker flips to blocked', () => {
    blockerState.current = {
      state: 'blocked',
      proceed: vi.fn(),
      reset: vi.fn(),
    };
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithRouter(<TestComponent isDirty />);
    expect(confirmSpy).toHaveBeenCalledWith('TEST MESSAGE');
    expect(blockerState.current.proceed).toHaveBeenCalled();
    expect(blockerState.current.reset).not.toHaveBeenCalled();
  });

  it('calls blocker.reset when the operator cancels the confirm dialog', () => {
    blockerState.current = {
      state: 'blocked',
      proceed: vi.fn(),
      reset: vi.fn(),
    };
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithRouter(<TestComponent isDirty />);
    expect(blockerState.current.reset).toHaveBeenCalled();
    expect(blockerState.current.proceed).not.toHaveBeenCalled();
  });
});
