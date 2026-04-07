import React, { useRef, useState, useEffect } from 'react';
import { View, TextInput, StyleSheet } from 'react-native';
import { colors, fonts } from '../theme/styles';

export default function ScanInput({ placeholder = 'SCAN BARCODE', onScan, disabled = false, autoFocus = true }) {
  const inputRef = useRef(null);
  const [value, setValue] = useState('');

  useEffect(() => {
    if (autoFocus && !disabled) {
      const timer = setTimeout(() => inputRef.current?.focus(), 100);
      return () => clearTimeout(timer);
    }
  }, [autoFocus, disabled]);

  const handleSubmit = () => {
    const trimmed = value.trim();
    if (trimmed && onScan) {
      onScan(trimmed);
    }
    setValue('');
    setTimeout(() => inputRef.current?.focus(), 50);
  };

  return (
    <View style={[styles.container, disabled && styles.disabled]}>
      <TextInput
        ref={inputRef}
        style={styles.input}
        placeholder={placeholder}
        placeholderTextColor={colors.textSecondary}
        value={value}
        onChangeText={setValue}
        onSubmitEditing={handleSubmit}
        editable={!disabled}
        autoFocus={autoFocus && !disabled}
        autoCapitalize="characters"
        autoCorrect={false}
        blurOnSubmit={false}
        returnKeyType="done"
        showSoftInputOnFocus={false}
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
