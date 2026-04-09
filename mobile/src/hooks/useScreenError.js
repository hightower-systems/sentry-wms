import { useState, useCallback } from 'react';

/**
 * Manages error + scanDisabled state pair used by every scan screen.
 *
 * Returns { error, scanDisabled, showError, clearError }
 *  - showError(msg)  → sets error message and disables scanner
 *  - clearError()    → clears message and re-enables scanner (use as ErrorPopup onDismiss)
 */
export default function useScreenError() {
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);

  const showError = useCallback((msg) => {
    setError(msg);
    setScanDisabled(true);
  }, []);

  const clearError = useCallback(() => {
    setError('');
    setScanDisabled(false);
  }, []);

  return { error, scanDisabled, showError, clearError };
}
