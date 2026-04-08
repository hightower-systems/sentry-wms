import React, { useRef, useState, useEffect } from 'react';
import { View, TextInput, StyleSheet } from 'react-native';
import { colors, fonts, radii } from '../theme/styles';

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
      try {
        if (inputRef.current && typeof inputRef.current.isFocused === 'function' && !inputRef.current.isFocused()) {
          inputRef.current.focus();
        }
      } catch {
        // Swallow focus errors on hardware scanner devices
      }
    }, 1000);
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
    bufferRef.current = text;
    if (timerRef.current) clearTimeout(timerRef.current);
    if (text.length > 0) {
      timerRef.current = setTimeout(() => {
        if (bufferRef.current === text && text.trim().length >= 3) {
          handleSubmit();
        }
      }, 100);
    }
  };

  const IGNORED_KEYS = ['Escape', 'GoBack', 'F1', 'F2', 'F3', 'F4', 'F5',
    'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12', 'Tab'];

  return (
    <View style={[styles.container, disabled && styles.disabled]}>
      <TextInput
        ref={inputRef}
        style={styles.input}
        placeholder={placeholder}
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
