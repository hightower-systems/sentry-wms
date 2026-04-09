import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { colors, fonts, radii, screenStyles, buttonStyles, doneStyles } from '../theme/styles';

export default function PickCompleteScreen({ navigation, route }) {
  const { total_picks = 0, total_orders = 0, shorts = 0 } = route.params || {};

  return (
    <View style={screenStyles.screen}>
      <View style={doneStyles.section}>
        <Text style={doneStyles.check}>&#10003;</Text>
        <Text style={doneStyles.title}>Batch complete!</Text>
        <Text style={doneStyles.detail}>
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
          style={[buttonStyles.buttonPrimary, { width: '100%', marginBottom: 12, paddingHorizontal: 32 }]}
          onPress={() => navigation.replace('PickScan')}
        >
          <Text style={buttonStyles.buttonPrimaryText}>START NEW BATCH</Text>
        </TouchableOpacity>

        <TouchableOpacity
          style={[buttonStyles.buttonSecondary, { width: '100%', paddingHorizontal: 32 }]}
          onPress={() => navigation.navigate('Home')}
        >
          <Text style={buttonStyles.buttonSecondaryText}>DONE</Text>
        </TouchableOpacity>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  summary: {
    width: '100%',
    marginBottom: 32,
  },
  summaryRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: 8,
    borderBottomWidth: 1,
    borderBottomColor: colors.cardBorder,
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
    color: colors.copper,
  },
});
