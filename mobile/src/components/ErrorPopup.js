import React from 'react';
import { Modal, View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { colors, fonts } from '../theme/styles';

export default function ErrorPopup({ visible, message, onDismiss }) {
  return (
    <Modal visible={visible} transparent animationType="fade">
      <View style={styles.overlay}>
        <View style={styles.card}>
          <TouchableOpacity style={styles.closeButton} onPress={onDismiss}>
            <Text style={styles.closeText}>X</Text>
          </TouchableOpacity>
          <Text style={styles.message}>{message}</Text>
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
    borderRadius: 8,
    padding: 24,
    paddingTop: 40,
    width: '100%',
    maxWidth: 340,
    alignItems: 'center',
  },
  closeButton: {
    position: 'absolute',
    top: 8,
    right: 12,
    padding: 8,
    minWidth: 48,
    minHeight: 48,
    alignItems: 'center',
    justifyContent: 'center',
  },
  closeText: {
    fontFamily: fonts.mono,
    fontSize: 16,
    fontWeight: '700',
    color: colors.textMuted,
  },
  message: {
    fontSize: 16,
    color: colors.textPrimary,
    textAlign: 'center',
    lineHeight: 24,
  },
});
