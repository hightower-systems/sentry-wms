import React, { useState } from 'react';
import { View, Text, TouchableOpacity, TextInput, ScrollView, Modal, Pressable, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

export default function ShipScreen({ navigation }) {
  const [order, setOrder] = useState(null);
  const [lines, setLines] = useState([]);
  const [totalItems, setTotalItems] = useState(0);
  const [phase, setPhase] = useState('scan_order'); // scan_order | shipping | done
  const [carrier, setCarrier] = useState('');
  const [isCustomCarrier, setIsCustomCarrier] = useState(false);
  const [showCarrierPicker, setShowCarrierPicker] = useState(false);
  const [tracking, setTracking] = useState('');
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);

  const CARRIERS = ['UPS', 'FedEx', 'USPS', 'DHL', 'Other'];

  const handleScanOrder = async (barcode) => {
    try {
      const resp = await client.get(`/api/shipping/order/${encodeURIComponent(barcode)}`);
      const data = resp.data;
      setOrder(data.sales_order);
      setLines(data.lines || []);
      setTotalItems(data.total_items || 0);
      setPhase('shipping');
    } catch (err) {
      setError(err.response?.data?.error || 'Order not found');
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
      setPhase('done');
    } catch (err) {
      setError(err.response?.data?.error || 'Shipment failed');
      setScanDisabled(true);
    }
  };

  const resetScreen = () => {
    setOrder(null);
    setLines([]);
    setTotalItems(0);
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
        <Text style={styles.headerTitle}>SHIP</Text>
        <View style={{ width: 32 }} />
      </View>

      <ScrollView style={styles.content} contentContainerStyle={styles.contentInner} keyboardShouldPersistTaps="handled">
        {phase === 'scan_order' && (
          <ScanInput placeholder="SCAN ORDER" onScan={handleScanOrder} disabled={scanDisabled} />
        )}

        {phase === 'shipping' && (
          <>
            <View style={styles.orderInfo}>
              <Text style={styles.soNumber}>{order.so_number}</Text>
              <Text style={styles.customer}>{order.customer_name}</Text>
              <Text style={styles.statusLabel}>
                {order.status === 'PACKED' ? 'PACKED - READY TO SHIP' : 'READY TO SHIP'}
              </Text>
            </View>

            <View style={styles.summaryRow}>
              <View style={styles.summaryItem}>
                <Text style={styles.summaryValue}>{lines.length}</Text>
                <Text style={styles.summaryLabel}>LINES</Text>
              </View>
              <View style={styles.summaryItem}>
                <Text style={styles.summaryValue}>{totalItems}</Text>
                <Text style={styles.summaryLabel}>UNITS</Text>
              </View>
            </View>

            <Text style={styles.fieldLabel}>CARRIER</Text>
            <TouchableOpacity style={styles.pickerBtn} onPress={() => setShowCarrierPicker(true)}>
              <Text style={[styles.pickerText, !carrier && { color: colors.textSecondary }]}>
                {carrier || 'Select carrier...'}
              </Text>
              <Text style={{ color: colors.textSecondary }}>&#9662;</Text>
            </TouchableOpacity>
            {isCustomCarrier && (
              <TextInput
                style={styles.textInput}
                value={carrier}
                onChangeText={setCarrier}
                placeholder="Enter carrier name"
                placeholderTextColor={colors.textSecondary}
                autoFocus
              />
            )}

            <Text style={styles.fieldLabel}>TRACKING NUMBER</Text>
            <TextInput
              style={styles.textInput}
              value={tracking}
              onChangeText={setTracking}
              placeholder="Enter tracking number"
              placeholderTextColor={colors.textSecondary}
              autoCapitalize="characters"
            />

            <TouchableOpacity style={styles.buttonPrimary} onPress={handleShip}>
              <Text style={styles.buttonPrimaryText}>SHIP</Text>
            </TouchableOpacity>
          </>
        )}

        {phase === 'done' && (
          <View style={styles.doneContainer}>
            <Text style={styles.doneIcon}>&#10003;</Text>
            <Text style={styles.doneTitle}>Order {order.so_number} shipped!</Text>
            <Text style={styles.doneDetail}>{carrier} - {tracking}</Text>
            <TouchableOpacity style={styles.buttonPrimary} onPress={resetScreen}>
              <Text style={styles.buttonPrimaryText}>SHIP ANOTHER ORDER</Text>
            </TouchableOpacity>
            <TouchableOpacity style={styles.buttonSecondary} onPress={() => navigation.goBack()}>
              <Text style={styles.buttonSecondaryText}>DONE</Text>
            </TouchableOpacity>
          </View>
        )}
      </ScrollView>

      <Modal visible={showCarrierPicker} transparent animationType="fade">
        <Pressable style={styles.pickerOverlay} onPress={() => setShowCarrierPicker(false)}>
          <View style={styles.pickerCard}>
            <Text style={styles.pickerTitle}>SELECT CARRIER</Text>
            {CARRIERS.map((c) => (
              <TouchableOpacity
                key={c}
                style={[styles.pickerOption, carrier === c && styles.pickerOptionActive]}
                onPress={() => {
                  if (c === 'Other') {
                    setCarrier('');
                    setIsCustomCarrier(true);
                    setShowCarrierPicker(false);
                  } else {
                    setCarrier(c);
                    setIsCustomCarrier(false);
                    setShowCarrierPicker(false);
                  }
                }}
              >
                <Text style={[styles.pickerOptionText, carrier === c && styles.pickerOptionTextActive]}>{c}</Text>
              </TouchableOpacity>
            ))}
          </View>
        </Pressable>
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
  backBtn: { padding: 4, minWidth: 32, minHeight: 48, justifyContent: 'center' },
  backText: { fontSize: 22, color: colors.textPrimary },
  headerTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, letterSpacing: 0.5 },
  content: { flex: 1 },
  contentInner: { padding: 16 },
  orderInfo: { marginBottom: 16 },
  soNumber: { fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary },
  customer: { fontSize: 13, color: colors.textMuted, marginTop: 2 },
  statusLabel: { fontFamily: fonts.mono, fontSize: 12, color: colors.success, letterSpacing: 0.3, marginTop: 4 },
  summaryRow: {
    flexDirection: 'row', gap: 12, marginBottom: 16,
  },
  summaryItem: {
    flex: 1, borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    padding: 12, alignItems: 'center',
  },
  summaryValue: { fontFamily: fonts.mono, fontSize: 20, fontWeight: '700', color: colors.textPrimary },
  summaryLabel: { fontFamily: fonts.mono, fontSize: 10, color: colors.textMuted, letterSpacing: 0.3, marginTop: 2 },
  fieldLabel: {
    fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted,
    letterSpacing: 0.3, marginBottom: 4, marginTop: 12,
  },
  textInput: {
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    paddingHorizontal: 12, paddingVertical: 10, fontSize: 14,
    color: colors.textPrimary, backgroundColor: colors.background, minHeight: 48, marginBottom: 8,
  },
  doneContainer: { alignItems: 'center', paddingTop: 40 },
  doneIcon: { fontSize: 48, color: colors.success, marginBottom: 16 },
  doneTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, marginBottom: 4 },
  doneDetail: { fontFamily: fonts.mono, fontSize: 13, color: colors.textMuted, marginBottom: 24 },
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48, marginTop: 16, width: '100%',
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  buttonSecondary: {
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48, marginTop: 8, width: '100%',
  },
  buttonSecondaryText: { color: colors.textPrimary, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  pickerBtn: {
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    paddingHorizontal: 12, paddingVertical: 12, minHeight: 48, marginBottom: 8,
    flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center',
    backgroundColor: colors.background,
  },
  pickerText: { fontSize: 14, color: colors.textPrimary, fontFamily: fonts.mono },
  pickerOverlay: {
    flex: 1, backgroundColor: 'rgba(0,0,0,0.4)',
    justifyContent: 'center', alignItems: 'center', padding: 32,
  },
  pickerCard: {
    backgroundColor: colors.background, borderRadius: 12, padding: 20, width: '100%',
    borderWidth: 1, borderColor: colors.border,
  },
  pickerTitle: { fontFamily: fonts.mono, fontSize: 12, fontWeight: '700', color: colors.textMuted, letterSpacing: 0.5, marginBottom: 12 },
  pickerOption: {
    padding: 14, borderRadius: 8, borderWidth: 1, borderColor: colors.border, marginBottom: 8,
  },
  pickerOptionActive: { borderColor: colors.accentRed, backgroundColor: '#fdf6f4' },
  pickerOptionText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textPrimary },
  pickerOptionTextActive: { color: colors.accentRed },
});
