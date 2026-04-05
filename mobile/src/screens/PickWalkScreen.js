import React, { useState, useEffect } from 'react';
import { View, Text, TouchableOpacity, TextInput, ScrollView, Modal, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

export default function PickWalkScreen({ navigation, route }) {
  const { batch_id, batch } = route.params;
  const [task, setTask] = useState(null);
  const [pickNumber, setPickNumber] = useState(0);
  const [totalPicks, setTotalPicks] = useState(0);
  const [totalOrders, setTotalOrders] = useState(0);
  const [ordersExpanded, setOrdersExpanded] = useState(false);
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);
  const [showShortModal, setShowShortModal] = useState(false);
  const [shortQty, setShortQty] = useState('0');

  useEffect(() => {
    if (batch) {
      setTotalPicks(batch.total_picks || 0);
      setTotalOrders(batch.total_orders || 0);
    }
    loadNextTask();
  }, []);

  const loadNextTask = async () => {
    try {
      const resp = await client.get(`/api/picking/batch/${batch_id}/next`);
      if (resp.data.message === 'All tasks complete') {
        navigation.replace('PickComplete', {
          batch_id,
          total_picks: totalPicks,
          total_orders: totalOrders,
        });
        return;
      }
      setTask(resp.data);
      setPickNumber(resp.data.pick_number || pickNumber + 1);
      if (resp.data.total_picks) setTotalPicks(resp.data.total_picks);
      if (resp.data.total_orders) setTotalOrders(resp.data.total_orders);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to load next task');
      setScanDisabled(true);
    }
  };

  const handleScan = async (barcode) => {
    if (!task) return;

    try {
      await client.post('/api/picking/confirm', {
        pick_task_id: task.pick_task_id,
        scanned_barcode: barcode,
        quantity_picked: task.quantity_to_pick,
      });
      await loadNextTask();
    } catch (err) {
      const msg = err.response?.data?.error || 'Pick failed';
      if (msg.toLowerCase().includes('barcode') || msg.toLowerCase().includes('wrong') || msg.toLowerCase().includes('expected')) {
        setError(`Wrong item — expected ${task.sku}`);
      } else {
        setError(msg);
      }
      setScanDisabled(true);
    }
  };

  const handleShort = async () => {
    if (!task) return;
    const qty = parseInt(shortQty, 10);
    if (isNaN(qty) || qty < 0) return;

    try {
      await client.post('/api/picking/short', {
        pick_task_id: task.pick_task_id,
        quantity_available: qty,
      });
      setShowShortModal(false);
      setShortQty('0');
      await loadNextTask();
    } catch (err) {
      setShowShortModal(false);
      setError(err.response?.data?.error || 'Short pick failed');
      setScanDisabled(true);
    }
  };

  const contributingOrders = task?.contributing_orders || [];

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <Text style={styles.headerTitle}>
          PICK {pickNumber} OF {totalPicks}
        </Text>
        <Text style={styles.headerOrders}>{totalOrders} order{totalOrders !== 1 ? 's' : ''}</Text>
      </View>

      {task && (
        <ScrollView style={styles.content} contentContainerStyle={styles.contentInner}>
          {/* Bin target card */}
          <View style={styles.binCard}>
            <Text style={styles.binLabel}>GO TO BIN</Text>
            <Text style={styles.binCode}>{task.bin_code}</Text>
            {task.zone && (
              <Text style={styles.binZone}>
                {task.zone}{task.aisle ? ` · AISLE ${task.aisle}` : ''}
              </Text>
            )}
          </View>

          {/* Item card */}
          <View style={styles.itemCard}>
            <View style={styles.itemCardInner}>
              <View style={{ flex: 1 }}>
                <Text style={styles.itemLabel}>ITEM</Text>
                <Text style={styles.sku}>{task.sku}</Text>
                <Text style={styles.itemName}>{task.item_name}</Text>
              </View>
              <View style={styles.qtySection}>
                <Text style={styles.itemLabel}>QTY</Text>
                <Text style={styles.qty}>{task.quantity_to_pick}</Text>
              </View>
            </View>
          </View>

          {/* Contributing orders */}
          {contributingOrders.length > 1 && (
            <TouchableOpacity
              style={styles.ordersToggle}
              onPress={() => setOrdersExpanded(!ordersExpanded)}
            >
              <Text style={styles.ordersToggleText}>
                FOR {contributingOrders.length} ORDERS {ordersExpanded ? '▼' : '▶'}
              </Text>
            </TouchableOpacity>
          )}
          {ordersExpanded && contributingOrders.map((order, i) => (
            <View key={i} style={styles.orderRow}>
              <Text style={styles.orderSo}>{order.so_number}</Text>
              <Text style={styles.orderQty}>{order.quantity}</Text>
            </View>
          ))}

          {/* Scan input */}
          <ScanInput
            placeholder="SCAN ITEM"
            onScan={handleScan}
            disabled={scanDisabled}
          />

          {/* Short pick button */}
          <TouchableOpacity
            style={styles.buttonSecondary}
            onPress={() => {
              setShortQty('0');
              setShowShortModal(true);
            }}
          >
            <Text style={styles.buttonSecondaryText}>SHORT PICK</Text>
          </TouchableOpacity>
        </ScrollView>
      )}

      {/* Short pick modal */}
      <Modal visible={showShortModal} transparent animationType="fade">
        <View style={styles.modalOverlay}>
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>SHORT PICK</Text>
            <Text style={styles.modalSubtitle}>
              Expected: {task?.quantity_to_pick} — Enter actual quantity available:
            </Text>
            <TextInput
              style={styles.shortInput}
              value={shortQty}
              onChangeText={setShortQty}
              keyboardType="number-pad"
              autoFocus
            />
            <View style={styles.modalActions}>
              <TouchableOpacity style={styles.buttonPrimary} onPress={handleShort}>
                <Text style={styles.buttonPrimaryText}>CONFIRM</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={styles.buttonSecondary}
                onPress={() => setShowShortModal(false)}
              >
                <Text style={styles.buttonSecondaryText}>CANCEL</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>

      <ErrorPopup
        visible={!!error}
        message={error}
        onDismiss={() => {
          setError('');
          setScanDisabled(false);
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  header: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    paddingHorizontal: 16, paddingTop: 52, paddingBottom: 12,
    borderBottomWidth: 2, borderBottomColor: colors.accentRed,
  },
  headerTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, letterSpacing: 0.5 },
  headerOrders: { fontFamily: fonts.mono, fontSize: 12, color: colors.textMuted },
  content: { flex: 1 },
  contentInner: { padding: 16 },

  binCard: {
    borderWidth: 1.5, borderColor: colors.accentRed, borderRadius: 8,
    padding: 20, marginBottom: 16, alignItems: 'center',
  },
  binLabel: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3, marginBottom: 4 },
  binCode: { fontFamily: fonts.mono, fontSize: 30, fontWeight: '700', color: colors.accentRed },
  binZone: { fontFamily: fonts.mono, fontSize: 12, color: colors.copper, letterSpacing: 0.3, marginTop: 4, textTransform: 'uppercase' },

  itemCard: {
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    padding: 16, marginBottom: 16,
  },
  itemCardInner: { flexDirection: 'row', alignItems: 'flex-start', justifyContent: 'space-between' },
  itemLabel: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3, marginBottom: 2 },
  sku: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textPrimary },
  itemName: { fontSize: 13, color: colors.textMuted, marginTop: 2 },
  qtySection: { alignItems: 'flex-end' },
  qty: { fontFamily: fonts.mono, fontSize: 28, fontWeight: '700', color: colors.accentRed },

  ordersToggle: { paddingVertical: 8, marginBottom: 8 },
  ordersToggleText: { fontFamily: fonts.mono, fontSize: 12, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3 },
  orderRow: {
    flexDirection: 'row', justifyContent: 'space-between',
    paddingVertical: 6, paddingHorizontal: 12, marginBottom: 4,
    backgroundColor: '#fafaf8', borderRadius: 4,
  },
  orderSo: { fontFamily: fonts.mono, fontSize: 13, color: colors.textPrimary },
  orderQty: { fontFamily: fonts.mono, fontSize: 13, fontWeight: '700', color: colors.textPrimary },

  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48, marginBottom: 8,
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  buttonSecondary: {
    backgroundColor: colors.background, borderWidth: 1.5, borderColor: colors.border, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonSecondaryText: { color: colors.textMuted, fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', letterSpacing: 0.5 },

  modalOverlay: { flex: 1, backgroundColor: colors.overlay, justifyContent: 'center', alignItems: 'center', padding: 32 },
  modalCard: { backgroundColor: colors.background, borderRadius: 8, padding: 24, width: '100%', maxWidth: 320 },
  modalTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, marginBottom: 8 },
  modalSubtitle: { fontSize: 13, color: colors.textMuted, marginBottom: 16 },
  shortInput: {
    fontFamily: fonts.mono, fontSize: 24, fontWeight: '700', color: colors.textPrimary,
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    paddingHorizontal: 16, paddingVertical: 12, textAlign: 'center', minHeight: 48,
    marginBottom: 16,
  },
  modalActions: { gap: 8 },
});
