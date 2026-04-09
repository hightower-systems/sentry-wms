import React from 'react';
import { Modal, Pressable, View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { colors, fonts, radii } from '../theme/styles';

/**
 * Reusable standard/turbo mode picker modal.
 *
 * Props:
 *  - visible: boolean
 *  - onClose: () => void
 *  - title: string (e.g. "RECEIVE MODE", "COUNT MODE")
 *  - mode: 'standard' | 'turbo'
 *  - onChangeMode: (mode) => void
 *  - standardDesc: string
 *  - turboDesc: string
 *  - children: ReactNode (optional extra content below the mode options)
 */
export default function ModeSelector({
  visible, onClose, title, mode, onChangeMode,
  standardDesc, turboDesc, children,
}) {
  return (
    <Modal visible={visible} transparent animationType="fade">
      <Pressable style={styles.overlay} onPress={onClose}>
        <View style={styles.card}>
          <Text style={styles.title}>{title}</Text>
          <TouchableOpacity
            style={[styles.option, mode === 'standard' && styles.optionActive]}
            onPress={() => onChangeMode('standard')}
          >
            <Text style={[styles.optionLabel, mode === 'standard' && styles.optionLabelActive]}>STANDARD</Text>
            <Text style={styles.optionDesc}>{standardDesc}</Text>
          </TouchableOpacity>
          <TouchableOpacity
            style={[styles.option, mode === 'turbo' && styles.optionActive]}
            onPress={() => onChangeMode('turbo')}
          >
            <Text style={[styles.optionLabel, mode === 'turbo' && styles.optionLabelActive]}>TURBO</Text>
            <Text style={styles.optionDesc}>{turboDesc}</Text>
          </TouchableOpacity>
          {children}
        </View>
      </Pressable>
    </Modal>
  );
}

const styles = StyleSheet.create({
  overlay: {
    flex: 1, backgroundColor: colors.overlay,
    justifyContent: 'flex-start', alignItems: 'flex-end',
    paddingTop: 100, paddingRight: 16,
  },
  card: {
    backgroundColor: colors.background, borderRadius: radii.card, padding: 16, minWidth: 220,
    borderWidth: 1, borderColor: colors.cardBorder,
  },
  title: {
    fontFamily: fonts.mono, fontSize: 12, fontWeight: '700',
    color: colors.textMuted, letterSpacing: 0.5, marginBottom: 12,
  },
  option: {
    padding: 12, borderRadius: radii.badge, borderWidth: 1,
    borderColor: colors.cardBorder, marginBottom: 8,
  },
  optionActive: { borderColor: colors.accentRed, backgroundColor: '#fdf6f4' },
  optionLabel: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  optionLabelActive: { color: colors.accentRed },
  optionDesc: { fontSize: 12, color: colors.textMuted, marginTop: 2 },
});
