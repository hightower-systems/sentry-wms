/**
 * ScanSettingsContext — app-wide scan mode management.
 *
 * Wraps useScanSettings and ChainwayScanner into a single provider.
 * When mode is 'intent', the native BroadcastReceiver is active.
 * When mode is 'keyboard', the receiver is stopped.
 *
 * Any component can subscribe to intent scans via the onIntentScan
 * callback ref — ScanInput uses this to route intent scans into
 * the same onScan handler as keyboard scans.
 */

import React, { createContext, useContext, useEffect, useRef, useCallback } from 'react';
import ChainwayScanner from '../native/ChainwayScanner';
import useScanSettings from '../hooks/useScanSettings';

const ScanSettingsContext = createContext(null);

export function ScanSettingsProvider({ children }) {
  const settings = useScanSettings();
  const { mode, intentAction, intentExtra, loaded } = settings;

  // Ref that holds the current active scan handler (set by whichever ScanInput is focused)
  const activeScanHandlerRef = useRef(null);

  // Manage native listener lifecycle based on mode
  useEffect(() => {
    if (!loaded) return;

    if (mode === 'intent' && ChainwayScanner.isAvailable) {
      ChainwayScanner.startListening(intentAction, intentExtra);

      const sub = ChainwayScanner.addListener((barcode) => {
        if (activeScanHandlerRef.current) {
          activeScanHandlerRef.current(barcode);
        }
      });

      return () => {
        sub.remove();
        ChainwayScanner.stopListening();
      };
    } else {
      // Ensure stopped when switching back to keyboard
      ChainwayScanner.stopListening();
    }
  }, [mode, intentAction, intentExtra, loaded]);

  // Function for ScanInput to register itself as the active handler
  const registerScanHandler = useCallback((handler) => {
    activeScanHandlerRef.current = handler;
  }, []);

  const unregisterScanHandler = useCallback((handler) => {
    // Only unregister if this handler is still the active one
    if (activeScanHandlerRef.current === handler) {
      activeScanHandlerRef.current = null;
    }
  }, []);

  const value = {
    ...settings,
    scannerAvailable: ChainwayScanner.isAvailable,
    registerScanHandler,
    unregisterScanHandler,
  };

  return (
    <ScanSettingsContext.Provider value={value}>
      {children}
    </ScanSettingsContext.Provider>
  );
}

export function useScanSettingsContext() {
  return useContext(ScanSettingsContext);
}
