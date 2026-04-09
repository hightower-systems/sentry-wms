import { useState, useCallback, useRef } from 'react';

/**
 * Manages error + scanDisabled state pair used by every scan screen.
 *
 * Returns { error, scanDisabled, showError, clearError, errorRef }
 *  - showError(msg)  → sets error message and disables scanner
 *  - clearError()    → clears message and re-enables scanner (use as ErrorPopup onDismiss)
 *  - errorRef        → synchronous ref tracking error state (for scan queue pausing)
 */
export default function useScreenError() {
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);
  const errorRef = useRef(false);

  const showError = useCallback((msg) => {
    setError(msg);
    setScanDisabled(true);
    errorRef.current = true;
  }, []);

  const clearError = useCallback(() => {
    setError('');
    setScanDisabled(false);
    errorRef.current = false;
  }, []);

  return { error, scanDisabled, showError, clearError, errorRef };
}
