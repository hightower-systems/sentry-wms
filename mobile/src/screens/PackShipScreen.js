import React, { useState } from 'react';
import { View, Text, TouchableOpacity, TextInput, ScrollView, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import client from '../api/client';
import { colors, fonts, radii } from '../theme/styles';

export default function PackShipScreen({ navigation }) {
  const [order, setOrder] = useState(null);
  const [items, setItems] = useState([]);
  const [phase, setPhase] = useState('scan_order'); // scan_order | packing | shipping | done
  const [carrier, setCarrier] = useState('');
  const [tracking, setTracking] = useState('');
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);

  const handleScanOrder = async (barcode) => {
    try {
      const resp = await client.get(`/api/packing/order/${encodeURIComponent(barcode)}`);
      setOrder(resp.data.order || resp.data);
      setItems((resp.data.items || []).map((item) => ({ ...item, verified: 0 })));
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
      // Update verified count for this item
      setItems((prev) => prev.map((item) => {
        if (item.item_id === resp.data.item_id) {
          return { ...item, verified: (item.verified || 0) + 1 };
        }
        return item;
      }));
    } catch (err) {
      const msg = err.response?.data?.error || 'Verification failed';
      setError(msg);
      setScanDisabled(true);
    }
  };

  const allVerified = items.length > 0 && items.every(
    (item) => (item.verified || 0) >= (item.quantity_picked || item.quantity_ordered)
  );

  const handleCompletePack = async () => {
    try {
      await client.post('/api/packing/complete', { so_id: order.so_id });
      setPhase('shipping');
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to complete pack');
      setScanDisabled(true);
    }
  };

  const handleShip = async () => {
    if (!carrier.trim() || !tracking.trim()) {
      setError('Carrier and tracking number are required');
      setScanDisabled(true);
      return;
    }
    try {
      await client.post('/api/shipping/fulfill', {
        so_id: order.so_id,
        tracking_number: tracking.trim(),
        carrier: carrier.trim(),
        ship_method: order.ship_method || 'GROUND',
      });
      resetScreen();
    } catch (err) {
      setError(err.response?.data?.error || 'Shipment failed');
      setScanDisabled(true);
    }
  };

  const resetScreen = () => {
    setOrder(null);
    setItems([]);
    setPhase('scan_order');
    setCarrier('');
    setTracking('');
  };

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>PACK / SHIP</Text>
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

        {phase === 'shipping' && (
          <>
            <View style={styles.orderInfo}>
              <Text style={styles.soNumber}>{order.so_number}</Text>
              <Text style={styles.packedLabel}>PACKED - READY TO SHIP</Text>
            </View>

            <Text style={styles.fieldLabel}>CARRIER</Text>
            <TextInput
              style={styles.textInput}
              value={carrier}
              onChangeText={setCarrier}
              placeholder="e.g. UPS, FedEx, USPS"
              placeholderTextColor={colors.textPlaceholder}
            />

            <Text style={styles.fieldLabel}>TRACKING NUMBER</Text>
            <TextInput
              style={styles.textInput}
              value={tracking}
              onChangeText={setTracking}
              placeholder="Enter tracking number"
              placeholderTextColor={colors.textPlaceholder}
              autoCapitalize="characters"
            />

            <TouchableOpacity style={styles.buttonPrimary} onPress={handleShip}>
              <Text style={styles.buttonPrimaryText}>SHIP</Text>
            </TouchableOpacity>
          </>
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
  packedLabel: { fontFamily: fonts.mono, fontSize: 12, color: colors.success, letterSpacing: 0.3, marginTop: 4 },
  itemRow: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    borderWidth: 1, borderColor: colors.cardBorder, borderRadius: radii.card,
    backgroundColor: colors.cardBg, padding: 12, marginBottom: 8, minHeight: 48,
  },
  itemRowComplete: { borderColor: colors.success, backgroundColor: '#f0f9f0' },
  sku: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textPrimary },
  itemName: { fontSize: 12, color: colors.textMuted, marginTop: 2 },
  itemQty: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  itemQtyText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  itemQtyComplete: { color: colors.success },
  checkIcon: { fontSize: 16, color: colors.success },
  fieldLabel: {
    fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted,
    letterSpacing: 0.3, marginBottom: 4, marginTop: 12,
  },
  textInput: {
    borderWidth: 1, borderColor: colors.inputBorder, borderRadius: radii.input,
    paddingHorizontal: 12, paddingVertical: 10, fontSize: 14,
    color: colors.textPrimary, backgroundColor: colors.inputBg, minHeight: 48, marginBottom: 8,
  },
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: radii.button,
    paddingVertical: 14, alignItems: 'center', minHeight: 48, marginTop: 16,
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
});
