import React, { useRef, useState, useEffect } from 'react';
import { View, TextInput, StyleSheet } from 'react-native';
import { colors, fonts } from '../theme/styles';

export default function ScanInput({ placeholder = 'SCAN BARCODE', onScan, disabled = false, autoFocus = true }) {
  const inputRef = useRef(null);
  const [value, setValue] = useState('');
  const bufferRef = useRef('');
  const timerRef = useRef(null);

  useEffect(() => {
    if (autoFocus && !disabled) {
      const timer = setTimeout(() => inputRef.current?.focus(), 100);
      return () => clearTimeout(timer);
    }
  }, [autoFocus, disabled]);

  // Re-focus input when it loses focus (hardware scanner can steal focus)
  useEffect(() => {
    if (disabled) return;
    const interval = setInterval(() => {
      if (!inputRef.current?.isFocused?.()) {
        inputRef.current?.focus();
      }
    }, 500);
    return () => clearInterval(interval);
  }, [disabled]);

  const handleSubmit = () => {
    const trimmed = value.trim();
    setValue('');
    bufferRef.current = '';
    if (trimmed && onScan) {
      onScan(trimmed);
    }
    setTimeout(() => inputRef.current?.focus(), 50);
  };

  const handleChangeText = (text) => {
    setValue(text);
    // Hardware scanners send characters rapidly then a newline.
    // Buffer rapid input and auto-submit after a brief pause
    // in case the scanner doesn't send Enter/Return.
    bufferRef.current = text;
    if (timerRef.current) clearTimeout(timerRef.current);
    if (text.length > 0) {
      timerRef.current = setTimeout(() => {
        // If the value hasn't changed in 100ms and is non-empty,
        // the scanner likely finished sending. Auto-submit.
        if (bufferRef.current === text && text.trim().length >= 3) {
          handleSubmit();
        }
      }, 100);
    }
  };

  // Keys the C6000 hardware scanner can emit that should be swallowed
  const IGNORED_KEYS = ['Escape', 'GoBack', 'F1', 'F2', 'F3', 'F4', 'F5',
    'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12', 'Tab'];

  return (
    <View style={[styles.container, disabled && styles.disabled]}>
      <TextInput
        ref={inputRef}
        style={styles.input}
        placeholder={placeholder}
        placeholderTextColor={colors.textSecondary}
        value={value}
        onChangeText={handleChangeText}
        onSubmitEditing={handleSubmit}
        onKeyPress={(e) => {
          // Prevent scanner extra keys from triggering navigation or focus loss
          if (IGNORED_KEYS.includes(e.nativeEvent.key)) {
            e.preventDefault?.();
            e.stopPropagation?.();
          }
        }}
        editable={!disabled}
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
    backgroundColor: colors.background,
    borderWidth: 1.5,
    borderColor: colors.accentRed,
    borderRadius: 8,
    paddingHorizontal: 12,
    minHeight: 48,
    marginBottom: 16,
  },
  disabled: {
    backgroundColor: '#f5f5f5',
    borderColor: colors.border,
  },
  input: {
    flex: 1,
    fontFamily: fonts.mono,
    fontSize: 14,
    color: colors.textPrimary,
    paddingVertical: 12,
  },
});
