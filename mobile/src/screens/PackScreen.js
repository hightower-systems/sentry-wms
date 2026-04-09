import React, { useState } from 'react';
import { View, Text, TouchableOpacity, ScrollView, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ScreenHeader from '../components/ScreenHeader';
import ErrorPopup from '../components/ErrorPopup';
import useScreenError from '../hooks/useScreenError';
import client from '../api/client';
import { colors, fonts, radii, screenStyles, buttonStyles, listStyles } from '../theme/styles';

export default function PackScreen({ navigation }) {
  const [order, setOrder] = useState(null);
  const [items, setItems] = useState([]);
  const [phase, setPhase] = useState('scan_order'); // scan_order | packing | done
  const { error, scanDisabled, showError, clearError } = useScreenError();

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
      showError(err.response?.data?.error || 'Order not found');
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
      showError(err.response?.data?.error || 'Verification failed');
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
      showError(err.response?.data?.error || 'Failed to complete pack');
    }
  };

  const resetScreen = () => {
    setOrder(null);
    setItems([]);
    setPhase('scan_order');
  };

  return (
    <View style={screenStyles.screen}>
      <ScreenHeader title="PACK" onBack={() => navigation.goBack()} />

      <ScrollView style={screenStyles.content} contentContainerStyle={screenStyles.contentInner} keyboardShouldPersistTaps="handled">
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
                <View key={idx} style={[listStyles.row, complete && styles.itemRowComplete]}>
                  <View style={{ flex: 1 }}>
                    <Text style={listStyles.sku}>{item.sku}</Text>
                    <Text style={listStyles.itemName}>{item.item_name}</Text>
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
              <TouchableOpacity style={[buttonStyles.buttonPrimary, { marginTop: 16, width: '100%' }]} onPress={handleCompletePack}>
                <Text style={buttonStyles.buttonPrimaryText}>COMPLETE PACK</Text>
              </TouchableOpacity>
            )}
          </>
        )}

        {phase === 'done' && (
          <View style={styles.doneContainer}>
            <Text style={styles.doneIcon}>&#10003;</Text>
            <Text style={styles.doneTitle}>Order {order.so_number} packed</Text>
            <TouchableOpacity style={[buttonStyles.buttonPrimary, { marginTop: 16, width: '100%' }]} onPress={resetScreen}>
              <Text style={buttonStyles.buttonPrimaryText}>PACK ANOTHER ORDER</Text>
            </TouchableOpacity>
            <TouchableOpacity style={[buttonStyles.buttonSecondary, { marginTop: 8, width: '100%' }]} onPress={() => navigation.goBack()}>
              <Text style={buttonStyles.buttonSecondaryText}>DONE</Text>
            </TouchableOpacity>
          </View>
        )}
      </ScrollView>

      <ErrorPopup
        visible={!!error}
        message={error}
        onDismiss={clearError}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  orderInfo: { marginBottom: 16 },
  soNumber: { fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary },
  customer: { fontSize: 13, color: colors.textMuted, marginTop: 2 },
  itemRowComplete: { borderColor: colors.success, backgroundColor: '#f0f9f0' },
  itemQty: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  itemQtyText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  itemQtyComplete: { color: colors.success },
  checkIcon: { fontSize: 16, color: colors.success },
  doneContainer: { alignItems: 'center', paddingTop: 40 },
  doneIcon: { fontSize: 48, color: colors.success, marginBottom: 16 },
  doneTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, marginBottom: 24 },
});
