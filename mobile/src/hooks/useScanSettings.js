/**
 * Scan settings hook — manages scan mode (keyboard vs intent) and
 * Chainway broadcast intent configuration.
 *
 * Persists to AsyncStorage so the setting survives app restarts.
 */

import { useState, useEffect, useCallback } from 'react';
import AsyncStorage from '@react-native-async-storage/async-storage';

const KEYS = {
  mode: 'sentry_scan_mode',           // 'keyboard' | 'intent'
  intentAction: 'sentry_scan_action',  // broadcast intent action string
  intentExtra: 'sentry_scan_extra',    // broadcast intent extra key
};

const DEFAULTS = {
  mode: 'keyboard',
  intentAction: 'com.chainway.sdk.barcode.BARCODE_DECODING_DATA',
  intentExtra: 'BARCODE_DATA_EXTRA',
};

export default function useScanSettings() {
  const [mode, setModeState] = useState(DEFAULTS.mode);
  const [intentAction, setIntentActionState] = useState(DEFAULTS.intentAction);
  const [intentExtra, setIntentExtraState] = useState(DEFAULTS.intentExtra);
  const [loaded, setLoaded] = useState(false);

  // Load persisted settings
  useEffect(() => {
    (async () => {
      try {
        const [m, a, e] = await Promise.all([
          AsyncStorage.getItem(KEYS.mode),
          AsyncStorage.getItem(KEYS.intentAction),
          AsyncStorage.getItem(KEYS.intentExtra),
        ]);
        if (m === 'keyboard' || m === 'intent') setModeState(m);
        if (a) setIntentActionState(a);
        if (e) setIntentExtraState(e);
      } catch {
        // Defaults are fine
      }
      setLoaded(true);
    })();
  }, []);

  const setMode = useCallback(async (val) => {
    setModeState(val);
    await AsyncStorage.setItem(KEYS.mode, val).catch(() => {});
  }, []);

  const setIntentAction = useCallback(async (val) => {
    setIntentActionState(val);
    await AsyncStorage.setItem(KEYS.intentAction, val).catch(() => {});
  }, []);

  const setIntentExtra = useCallback(async (val) => {
    setIntentExtraState(val);
    await AsyncStorage.setItem(KEYS.intentExtra, val).catch(() => {});
  }, []);

  return {
    loaded,
    mode,
    setMode,
    intentAction,
    setIntentAction,
    intentExtra,
    setIntentExtra,
  };
}
