import React from 'react';
import { Modal, View, Text, Pressable, FlatList, StyleSheet } from 'react-native';
import { colors, fonts, radii } from '../theme/styles';

export default function WarehouseSelector({ visible, warehouses, selected, onSelect, onClose }) {
  const renderItem = ({ item }) => {
    const isSelected = item.id === selected;
    return (
      <Pressable
        style={({ pressed }) => [styles.item, isSelected && styles.itemSelected, pressed && styles.itemPressed]}
        onPress={() => onSelect(item.id)}
        android_disableSound={false}
      >
        <Text style={[styles.code, isSelected && styles.codeSelected]}>{item.code}</Text>
        <Text style={[styles.name, isSelected && styles.nameSelected]}>{item.name}</Text>
      </Pressable>
    );
  };

  return (
    <Modal visible={visible} transparent animationType="fade">
      <Pressable style={styles.overlay} onPress={onClose}>
        <Pressable style={styles.card} onPress={() => {}}>
          <Text style={styles.title}>SELECT WAREHOUSE</Text>
          <FlatList
            data={warehouses}
            keyExtractor={(item) => String(item.id)}
            renderItem={renderItem}
            style={styles.list}
          />
        </Pressable>
      </Pressable>
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
    padding: 20,
    width: '100%',
    maxWidth: 340,
    maxHeight: '60%',
    borderWidth: 1,
    borderColor: colors.cardBorder,
  },
  title: {
    fontFamily: fonts.mono,
    fontSize: 12,
    fontWeight: '700',
    color: colors.textMuted,
    letterSpacing: 0.5,
    marginBottom: 16,
    textAlign: 'center',
  },
  list: {
    flexGrow: 0,
  },
  item: {
    borderWidth: 1,
    borderColor: colors.cardBorder,
    borderRadius: radii.card,
    padding: 16,
    marginBottom: 8,
    minHeight: 48,
    justifyContent: 'center',
    backgroundColor: colors.cardBg,
  },
  itemSelected: {
    borderColor: colors.accentRed,
    borderWidth: 1.5,
    backgroundColor: colors.background,
  },
  code: {
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    color: colors.textPrimary,
    letterSpacing: 0.3,
  },
  codeSelected: {
    color: colors.accentRed,
  },
  name: {
    fontSize: 13,
    color: colors.textMuted,
    marginTop: 2,
  },
  nameSelected: {
    color: colors.textPrimary,
  },
  itemPressed: {
    opacity: 0.7,
    backgroundColor: colors.cardBorder,
  },
});
