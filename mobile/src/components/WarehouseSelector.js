import React from 'react';
import { Modal, View, Text, TouchableOpacity, FlatList, StyleSheet } from 'react-native';
import { colors, fonts } from '../theme/styles';

export default function WarehouseSelector({ visible, warehouses, selected, onSelect }) {
  const renderItem = ({ item }) => {
    const isSelected = item.id === selected;
    return (
      <TouchableOpacity
        style={[styles.item, isSelected && styles.itemSelected]}
        onPress={() => onSelect(item.id)}
      >
        <Text style={[styles.code, isSelected && styles.codeSelected]}>{item.code}</Text>
        <Text style={[styles.name, isSelected && styles.nameSelected]}>{item.name}</Text>
      </TouchableOpacity>
    );
  };

  return (
    <Modal visible={visible} transparent animationType="fade">
      <View style={styles.overlay}>
        <View style={styles.card}>
          <Text style={styles.title}>SELECT WAREHOUSE</Text>
          <FlatList
            data={warehouses}
            keyExtractor={(item) => String(item.id)}
            renderItem={renderItem}
            style={styles.list}
          />
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
    padding: 20,
    width: '100%',
    maxWidth: 340,
    maxHeight: '60%',
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
    borderColor: colors.border,
    borderRadius: 8,
    padding: 16,
    marginBottom: 8,
    minHeight: 48,
    justifyContent: 'center',
  },
  itemSelected: {
    borderColor: colors.accentRed,
    borderWidth: 1.5,
    backgroundColor: '#fdf8f7',
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
});
