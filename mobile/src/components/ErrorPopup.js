import React from 'react';
import { Modal, View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { colors, fonts, radii } from '../theme/styles';

export default function ErrorPopup({ visible, message, onDismiss }) {
  return (
    <Modal visible={visible} transparent animationType="fade">
      <View style={styles.overlay}>
        <View style={styles.card}>
          <Text style={styles.message}>{message}</Text>
          <TouchableOpacity style={styles.button} onPress={onDismiss}>
            <Text style={styles.buttonText}>DISMISS</Text>
          </TouchableOpacity>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  overlay: {
    flex: 1,
    backgroundColor: colors.overlay,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 32,
  },
  card: {
    backgroundColor: colors.background,
    borderRadius: radii.card,
    borderWidth: 1.5,
    borderColor: colors.accentRed,
    padding: 24,
    width: '100%',
    maxWidth: 320,
    alignItems: 'center',
  },
  message: {
    fontSize: 16,
    color: colors.accentRed,
    textAlign: 'center',
    lineHeight: 24,
    marginBottom: 20,
  },
  button: {
    backgroundColor: colors.accentRed,
    borderRadius: radii.button,
    paddingVertical: 12,
    paddingHorizontal: 32,
    alignItems: 'center',
    minHeight: 44,
    width: '100%',
  },
  buttonText: {
    color: colors.cream,
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '700',
    letterSpacing: 0.5,
  },
});
