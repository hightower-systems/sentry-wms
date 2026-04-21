import React, { useRef, useState, useEffect } from 'react';
import { View, TextInput, StyleSheet } from 'react-native';
import { colors, fonts, radii } from '../theme/styles';
import { useScanSettingsContext } from '../context/ScanSettingsContext';

export default function ScanInput({ placeholder = 'SCAN BARCODE', onScan, disabled = false, autoFocus = true, suppressRefocus = false }) {
  const inputRef = useRef(null);
  const [value, setValue] = useState('');
  const bufferRef = useRef('');
  const [processing, setProcessing] = useState(false);
  const scanSettings = useScanSettingsContext();

  // Register this ScanInput's onScan as the active intent handler
  // when the component is mounted and not disabled
  useEffect(() => {
    if (!scanSettings || scanSettings.mode !== 'intent' || disabled) return;
    const handler = (barcode) => {
      if (disabled || processing) return;
      const trimmed = barcode.replace(/[\r\n\s]+/g, '').trim();
      if (!trimmed || !onScan) return;
      setProcessing(true);
      Promise.resolve(onScan(trimmed)).finally(() => {
        setProcessing(false);
      });
    };
    scanSettings.registerScanHandler(handler);
    return () => scanSettings.unregisterScanHandler(handler);
  }, [scanSettings?.mode, onScan, disabled, processing]);

  useEffect(() => {
    if (autoFocus && !disabled && !processing && !suppressRefocus) {
      const timer = setTimeout(() => inputRef.current?.focus(), 100);
      return () => clearTimeout(timer);
    }
  }, [autoFocus, disabled, processing, suppressRefocus]);

  // Re-focus input when it loses focus (hardware scanner can steal focus)
  // Suppressed when another input (e.g. qty field) has focus
  useEffect(() => {
    if (disabled || processing || suppressRefocus) return;
    const interval = setInterval(() => {
      try {
        if (inputRef.current && typeof inputRef.current.isFocused === 'function' && !inputRef.current.isFocused()) {
          inputRef.current.focus();
        }
      } catch {
        // Swallow focus errors on hardware scanner devices
      }
    }, 1000);
    return () => clearInterval(interval);
  }, [disabled, processing, suppressRefocus]);

  const scanInFlightRef = useRef(false);

  const handleSubmit = () => {
    // Use bufferRef (synchronous) instead of value (async React state)
    // to avoid the C6000 race where Enter fires before the last onChangeText flushes
    const raw = bufferRef.current;
    const trimmed = raw.replace(/[\r\n\s]+/g, '').trim();

    setValue('');
    bufferRef.current = '';
    if (!trimmed || !onScan || scanInFlightRef.current) {
      setTimeout(() => inputRef.current?.focus(), 50);
      return;
    }

    scanInFlightRef.current = true;
    setProcessing(true);
    Promise.resolve(onScan(trimmed)).finally(() => {
      scanInFlightRef.current = false;
      setProcessing(false);
      setTimeout(() => inputRef.current?.focus(), 50);
    });
  };

  const handleChangeText = (text) => {
    // Strip control characters that hardware scanners may inject (keep printable chars only)
    const cleaned = text.replace(/[\r\n\t]/g, '');
    bufferRef.current = cleaned;
    setValue(cleaned);
    // NO auto-submit timer. Only process on Enter/Submit (onSubmitEditing).
    // C6000 scanners send characters one at a time; a timer causes partial submits.
  };

  const IGNORED_KEYS = ['Escape', 'GoBack', 'F1', 'F2', 'F3', 'F4', 'F5',
    'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12', 'Tab'];

  return (
    <View style={[styles.container, (disabled || processing) && styles.disabled]}>
      <TextInput
        ref={inputRef}
        style={styles.input}
        placeholder={processing ? 'PROCESSING...' : placeholder}
        placeholderTextColor={colors.textPlaceholder}
        value={value}
        onChangeText={handleChangeText}
        onSubmitEditing={handleSubmit}
        onKeyPress={(e) => {
          if (IGNORED_KEYS.includes(e.nativeEvent.key)) {
            e.preventDefault?.();
            e.stopPropagation?.();
          }
        }}
        editable={!disabled && !processing}
        autoFocus={autoFocus && !disabled}
        autoCapitalize="characters"
        autoCorrect={false}
        blurOnSubmit={false}
        returnKeyType="done"
        selectTextOnFocus
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.inputBg,
    borderWidth: 1.5,
    borderColor: colors.inputBorder,
    borderRadius: radii.input,
    paddingHorizontal: 12,
    minHeight: 44,
    marginBottom: 16,
  },
  disabled: {
    backgroundColor: '#f0ede6',
    borderColor: colors.cardBorder,
  },
  input: {
    flex: 1,
    fontFamily: fonts.mono,
    fontSize: 12,
    color: colors.textPrimary,
    letterSpacing: 1,
    paddingVertical: 10,
  },
});
