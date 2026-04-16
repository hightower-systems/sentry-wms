/**
 * ErrorBoundary component tests.
 *
 * Covers:
 * 1. Renders children when no error
 * 2. Catches errors and displays fallback UI
 * 3. Custom fallbackMessage is shown
 * 4. Reset button clears error and re-renders children
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ErrorBoundary from '../components/ErrorBoundary.jsx';

// Suppress console.error noise from React's error boundary logging
beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

function ProblemChild({ shouldThrow = true }) {
  if (shouldThrow) {
    throw new Error('Test error');
  }
  return <div>Child rendered OK</div>;
}

function GoodChild() {
  return <div>All good</div>;
}

describe('ErrorBoundary', () => {
  it('renders children when no error occurs', () => {
    render(
      <ErrorBoundary>
        <GoodChild />
      </ErrorBoundary>
    );
    expect(screen.getByText('All good')).toBeTruthy();
  });

  it('catches error and shows fallback UI', () => {
    render(
      <ErrorBoundary>
        <ProblemChild />
      </ErrorBoundary>
    );
    expect(screen.getByText('Something went wrong')).toBeTruthy();
    expect(screen.getByText('This section encountered an error. Try refreshing.')).toBeTruthy();
    expect(screen.getByText('Retry')).toBeTruthy();
  });

  it('shows custom fallbackMessage', () => {
    render(
      <ErrorBoundary fallbackMessage="Could not load inventory.">
        <ProblemChild />
      </ErrorBoundary>
    );
    expect(screen.getByText('Could not load inventory.')).toBeTruthy();
  });

  it('resets error state when Retry is clicked', () => {
    let shouldThrow = true;

    function ToggleChild() {
      if (shouldThrow) {
        throw new Error('boom');
      }
      return <div>Recovered</div>;
    }

    render(
      <ErrorBoundary>
        <ToggleChild />
      </ErrorBoundary>
    );

    expect(screen.getByText('Something went wrong')).toBeTruthy();

    // Fix the child before clicking retry
    shouldThrow = false;
    fireEvent.click(screen.getByText('Retry'));

    expect(screen.getByText('Recovered')).toBeTruthy();
  });
});
