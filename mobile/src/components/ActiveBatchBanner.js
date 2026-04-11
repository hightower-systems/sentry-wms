import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { colors, fonts, radii } from '../theme/styles';

export default function ActiveBatchBanner({ batch, onResume, onDismiss, onDelete }) {
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
        {onDelete && (
          <TouchableOpacity style={styles.deleteButton} onPress={onDelete}>
            <Text style={styles.deleteText}>Delete</Text>
          </TouchableOpacity>
        )}
        <TouchableOpacity style={styles.dismissButton} onPress={onDismiss}>
          <Text style={styles.dismissText}>Dismiss</Text>
        </TouchableOpacity>
        <TouchableOpacity style={styles.resumeButton} onPress={onResume}>
          <Text style={styles.resumeText}>RESUME</Text>
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
    gap: 8,
  },
  resumeButton: {
    flex: 1,
    borderWidth: 1.5,
    borderColor: colors.textPrimary,
    borderRadius: radii.button,
    paddingVertical: 10,
    minHeight: 40,
    alignItems: 'center',
    justifyContent: 'center',
  },
  resumeText: {
    fontFamily: fonts.mono,
    fontSize: 12,
    fontWeight: '700',
    color: colors.textPrimary,
    letterSpacing: 0.5,
  },
  dismissButton: {
    flex: 1,
    borderWidth: 1.5,
    borderColor: colors.cardBorder,
    borderRadius: radii.button,
    paddingVertical: 10,
    minHeight: 40,
    alignItems: 'center',
    justifyContent: 'center',
  },
  dismissText: {
    fontFamily: fonts.mono,
    color: colors.textMuted,
    fontSize: 12,
    fontWeight: '600',
  },
  deleteButton: {
    flex: 1,
    borderWidth: 1.5,
    borderColor: colors.accentRed,
    borderRadius: radii.button,
    paddingVertical: 10,
    minHeight: 40,
    alignItems: 'center',
    justifyContent: 'center',
  },
  deleteText: {
    fontFamily: fonts.mono,
    color: colors.accentRed,
    fontSize: 12,
    fontWeight: '600',
  },
});
