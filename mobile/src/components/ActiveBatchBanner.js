import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { colors, fonts } from '../theme/styles';

export default function ActiveBatchBanner({ batch, onResume, onDismiss }) {
  if (!batch) return null;

  return (
    <View style={styles.container}>
      <Text style={styles.message}>
        Resume pick batch? {batch.completed_picks} of {batch.total_picks} picks done
      </Text>
      <Text style={styles.detail}>
        {batch.total_orders} order{batch.total_orders !== 1 ? 's' : ''}
      </Text>
      <View style={styles.actions}>
        <TouchableOpacity style={styles.resumeButton} onPress={onResume}>
          <Text style={styles.resumeText}>RESUME</Text>
        </TouchableOpacity>
        <TouchableOpacity style={styles.dismissButton} onPress={onDismiss}>
          <Text style={styles.dismissText}>Dismiss</Text>
        </TouchableOpacity>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    backgroundColor: colors.background,
    borderWidth: 1.5,
    borderColor: colors.accentRed,
    borderRadius: 8,
    padding: 16,
    marginBottom: 16,
  },
  message: {
    fontSize: 15,
    fontWeight: '600',
    color: colors.textPrimary,
    marginBottom: 4,
  },
  detail: {
    fontFamily: fonts.mono,
    fontSize: 12,
    color: colors.textMuted,
    marginBottom: 12,
  },
  actions: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 16,
  },
  resumeButton: {
    backgroundColor: colors.accentRed,
    borderRadius: 8,
    paddingVertical: 10,
    paddingHorizontal: 20,
    minHeight: 48,
    justifyContent: 'center',
  },
  resumeText: {
    color: colors.cream,
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '700',
    letterSpacing: 0.5,
  },
  dismissButton: {
    paddingVertical: 10,
    paddingHorizontal: 12,
    minHeight: 48,
    justifyContent: 'center',
  },
  dismissText: {
    color: colors.textMuted,
    fontSize: 14,
  },
});
