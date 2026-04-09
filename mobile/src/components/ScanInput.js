import React, { useRef, useState, useEffect } from 'react';
import { View, TextInput, StyleSheet } from 'react-native';
import { colors, fonts, radii } from '../theme/styles';

export default function ScanInput({ placeholder = 'SCAN BARCODE', onScan, disabled = false, autoFocus = true }) {
  const inputRef = useRef(null);
  const [value, setValue] = useState('');
  const [processing, setProcessing] = useState(false);

  useEffect(() => {
    if (autoFocus && !disabled && !processing) {
      const timer = setTimeout(() => inputRef.current?.focus(), 100);
      return () => clearTimeout(timer);
    }
  }, [autoFocus, disabled, processing]);

  // Re-focus input when it loses focus (hardware scanner can steal focus)
  useEffect(() => {
    if (disabled || processing) return;
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
  }, [disabled, processing]);

  const scanInFlightRef = useRef(false);

  const handleSubmit = () => {
    // DEBUG: log raw value with char codes to detect invisible characters
    const charCodes = Array.from(value).map((c) => c.charCodeAt(0));
    console.log('[SCAN_DEBUG] raw value:', JSON.stringify(value), 'charCodes:', charCodes);

    // Strip ALL whitespace, carriage returns, newlines, and non-printable chars
    const trimmed = value.replace(/[\r\n\s]+/g, '').trim();
    console.log('[SCAN_DEBUG] trimmed value:', JSON.stringify(trimmed), 'length:', trimmed.length);

    setValue('');
    if (!trimmed || !onScan || scanInFlightRef.current) {
      console.log('[SCAN_DEBUG] SKIPPED — empty:', !trimmed, 'noHandler:', !onScan, 'inFlight:', scanInFlightRef.current);
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
        showSoftInputOnFocus={false}
        selectTextOnFocus
        contextMenuHidden
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
