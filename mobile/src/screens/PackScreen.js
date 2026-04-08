import React, { useState } from 'react';
import { View, Text, TouchableOpacity, ScrollView, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import client from '../api/client';
import { colors, fonts, radii } from '../theme/styles';

export default function PackScreen({ navigation }) {
  const [order, setOrder] = useState(null);
  const [items, setItems] = useState([]);
  const [phase, setPhase] = useState('scan_order'); // scan_order | packing | done
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);

  const handleScanOrder = async (barcode) => {
    try {
      const resp = await client.get(`/api/packing/order/${encodeURIComponent(barcode)}`);
      const data = resp.data;
      setOrder(data.sales_order || data.order || data);
      setItems(
        (data.lines || data.items || []).map((item) => ({
          ...item,
          verified: item.quantity_packed || 0,
        }))
      );
      setPhase('packing');
    } catch (err) {
      setError(err.response?.data?.error || 'Order not found');
      setScanDisabled(true);
    }
  };

  const handleScanItem = async (barcode) => {
    try {
      const resp = await client.post('/api/packing/verify', {
        so_id: order.so_id,
        scanned_barcode: barcode,
      });
      setItems((prev) =>
        prev.map((item) => {
          if (item.sku === resp.data.item?.sku || item.item_id === resp.data.item_id) {
            return { ...item, verified: (item.verified || 0) + (resp.data.item?.quantity_verified || 1) };
          }
          return item;
        })
      );
    } catch (err) {
      setError(err.response?.data?.error || 'Verification failed');
      setScanDisabled(true);
    }
  };

  const allVerified =
    items.length > 0 &&
    items.every((item) => (item.verified || 0) >= (item.quantity_picked || item.quantity_ordered));

  const handleCompletePack = async () => {
    try {
      await client.post('/api/packing/complete', { so_id: order.so_id });
      setPhase('done');
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to complete pack');
      setScanDisabled(true);
    }
  };

  const resetScreen = () => {
    setOrder(null);
    setItems([]);
    setPhase('scan_order');
  };

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>PACK</Text>
        <View style={{ width: 32 }} />
      </View>

      <ScrollView style={styles.content} contentContainerStyle={styles.contentInner} keyboardShouldPersistTaps="handled">
        {phase === 'scan_order' && (
          <ScanInput placeholder="SCAN ORDER" onScan={handleScanOrder} disabled={scanDisabled} />
        )}

        {phase === 'packing' && (
          <>
            <View style={styles.orderInfo}>
              <Text style={styles.soNumber}>{order.so_number}</Text>
              <Text style={styles.customer}>{order.customer_name}</Text>
            </View>

            <ScanInput placeholder="SCAN ITEM" onScan={handleScanItem} disabled={scanDisabled} />

            {items.map((item, idx) => {
              const expected = item.quantity_picked || item.quantity_ordered;
              const done = item.verified || 0;
              const complete = done >= expected;
              return (
                <View key={idx} style={[styles.itemRow, complete && styles.itemRowComplete]}>
                  <View style={{ flex: 1 }}>
                    <Text style={styles.sku}>{item.sku}</Text>
                    <Text style={styles.itemName}>{item.item_name}</Text>
                  </View>
                  <View style={styles.itemQty}>
                    <Text style={[styles.itemQtyText, complete && styles.itemQtyComplete]}>
                      {done}/{expected}
                    </Text>
                    {complete && <Text style={styles.checkIcon}>&#10003;</Text>}
                  </View>
                </View>
              );
            })}

            {allVerified && (
              <TouchableOpacity style={styles.buttonPrimary} onPress={handleCompletePack}>
                <Text style={styles.buttonPrimaryText}>COMPLETE PACK</Text>
              </TouchableOpacity>
            )}
          </>
        )}

        {phase === 'done' && (
          <View style={styles.doneContainer}>
            <Text style={styles.doneIcon}>&#10003;</Text>
            <Text style={styles.doneTitle}>Order {order.so_number} packed</Text>
            <TouchableOpacity style={styles.buttonPrimary} onPress={resetScreen}>
              <Text style={styles.buttonPrimaryText}>PACK ANOTHER ORDER</Text>
            </TouchableOpacity>
            <TouchableOpacity style={styles.buttonSecondary} onPress={() => navigation.goBack()}>
              <Text style={styles.buttonSecondaryText}>DONE</Text>
            </TouchableOpacity>
          </View>
        )}
      </ScrollView>

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
  },
  backBtn: { padding: 4, minWidth: 32, minHeight: 48, justifyContent: 'center' },
  backText: { fontSize: 22, color: colors.textPrimary },
  headerTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, letterSpacing: 0.5 },
  content: { flex: 1 },
  contentInner: { padding: 16 },
  orderInfo: { marginBottom: 16 },
  soNumber: { fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary },
  customer: { fontSize: 13, color: colors.textMuted, marginTop: 2 },
  itemRow: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    backgroundColor: colors.cardBg, borderWidth: 1, borderColor: colors.cardBorder, borderRadius: radii.card,
    padding: 12, marginBottom: 8, minHeight: 48,
  },
  itemRowComplete: { borderColor: colors.success, backgroundColor: '#f0f9f0' },
  sku: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textPrimary },
  itemName: { fontSize: 12, color: colors.textMuted, marginTop: 2 },
  itemQty: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  itemQtyText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  itemQtyComplete: { color: colors.success },
  checkIcon: { fontSize: 16, color: colors.success },
  doneContainer: { alignItems: 'center', paddingTop: 40 },
  doneIcon: { fontSize: 48, color: colors.success, marginBottom: 16 },
  doneTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, marginBottom: 24 },
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: radii.button,
    paddingVertical: 14, alignItems: 'center', minHeight: 48, marginTop: 16, width: '100%',
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  buttonSecondary: {
    backgroundColor: colors.background, borderWidth: 1.5, borderColor: colors.cardBorder, borderRadius: radii.button,
    paddingVertical: 14, alignItems: 'center', minHeight: 48, marginTop: 8, width: '100%',
  },
  buttonSecondaryText: { color: colors.textSecondary, fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', letterSpacing: 0.5 },
});
