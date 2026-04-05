import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { colors, fonts } from '../theme/styles';

export default function PickCompleteScreen({ navigation, route }) {
  const { total_picks = 0, total_orders = 0, shorts = 0 } = route.params || {};

  return (
    <View style={styles.screen}>
      <View style={styles.center}>
        <Text style={styles.checkmark}>&#10003;</Text>
        <Text style={styles.title}>Batch complete!</Text>
        <Text style={styles.subtitle}>
          {total_orders} order{total_orders !== 1 ? 's' : ''} ready for packing
        </Text>

        <View style={styles.summary}>
          <View style={styles.summaryRow}>
            <Text style={styles.summaryLabel}>Total picks</Text>
            <Text style={styles.summaryValue}>{total_picks}</Text>
          </View>
          {shorts > 0 && (
            <View style={styles.summaryRow}>
              <Text style={styles.summaryLabel}>Short picks</Text>
              <Text style={[styles.summaryValue, styles.shortValue]}>{shorts}</Text>
            </View>
          )}
        </View>

        <TouchableOpacity
          style={styles.buttonPrimary}
          onPress={() => navigation.replace('PickScan')}
        >
          <Text style={styles.buttonPrimaryText}>START NEW BATCH</Text>
        </TouchableOpacity>

        <TouchableOpacity
          style={styles.buttonSecondary}
          onPress={() => navigation.navigate('Home')}
        >
          <Text style={styles.buttonSecondaryText}>DONE</Text>
        </TouchableOpacity>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 32 },
  checkmark: {
    fontSize: 64,
    color: colors.success,
    marginBottom: 16,
  },
  title: {
    fontFamily: fonts.mono,
    fontSize: 22,
    fontWeight: '700',
    color: colors.textPrimary,
    marginBottom: 8,
  },
  subtitle: {
    fontSize: 15,
    color: colors.textMuted,
    marginBottom: 32,
  },
  summary: {
    width: '100%',
    marginBottom: 32,
  },
  summaryRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: 8,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  summaryLabel: {
    fontFamily: fonts.mono,
    fontSize: 13,
    color: colors.textMuted,
  },
  summaryValue: {
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  shortValue: {
    color: colors.warning,
  },
  buttonPrimary: {
    backgroundColor: colors.accentRed,
    borderRadius: 8,
    paddingVertical: 14,
    paddingHorizontal: 32,
    alignItems: 'center',
    minHeight: 48,
    width: '100%',
    marginBottom: 12,
  },
  buttonPrimaryText: {
    color: colors.cream,
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    letterSpacing: 0.5,
  },
  buttonSecondary: {
    backgroundColor: colors.background,
    borderWidth: 1.5,
    borderColor: colors.border,
    borderRadius: 8,
    paddingVertical: 14,
    paddingHorizontal: 32,
    alignItems: 'center',
    minHeight: 48,
    width: '100%',
  },
  buttonSecondaryText: {
    color: colors.textMuted,
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '600',
    letterSpacing: 0.5,
  },
});
