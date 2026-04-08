import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { colors, fonts, radii } from '../theme/styles';

export default function ActiveBatchBanner({ batch, onResume, onDismiss }) {
  if (!batch) return null;

  return (
    <View style={styles.container}>
      <Text style={styles.label}>ACTIVE BATCH</Text>
      <Text style={styles.message}>
        {batch.completed_picks} of {batch.total_picks} picks done
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
    backgroundColor: colors.cardBg,
    borderWidth: 1,
    borderColor: colors.cardBorder,
    borderLeftWidth: 4,
    borderLeftColor: colors.accentRed,
    borderRadius: radii.card,
    padding: 14,
    marginBottom: 16,
  },
  label: {
    fontFamily: fonts.mono,
    fontSize: 9,
    fontWeight: '700',
    color: colors.accentRed,
    letterSpacing: 1.5,
    marginBottom: 4,
  },
  message: {
    fontFamily: fonts.mono,
    fontSize: 13,
    color: colors.textPrimary,
    marginBottom: 2,
  },
  detail: {
    fontFamily: fonts.mono,
    fontSize: 11,
    color: colors.textMuted,
    marginBottom: 12,
  },
  actions: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 16,
  },
  resumeButton: {
    borderWidth: 1,
    borderColor: colors.textPrimary,
    borderRadius: 4,
    paddingVertical: 6,
    paddingHorizontal: 14,
  },
  resumeText: {
    fontFamily: fonts.mono,
    fontSize: 10,
    fontWeight: '700',
    color: colors.textPrimary,
    letterSpacing: 0.5,
  },
  dismissButton: {
    paddingVertical: 6,
    paddingHorizontal: 8,
  },
  dismissText: {
    color: colors.textMuted,
    fontSize: 12,
  },
});
