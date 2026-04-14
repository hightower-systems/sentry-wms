import React, { useState, useEffect } from 'react';
import { useScrollToTop } from '@react-navigation/native';
import { View, Text, TouchableOpacity, TextInput, ScrollView, Modal, Pressable, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ScreenHeader from '../components/ScreenHeader';
import ErrorPopup from '../components/ErrorPopup';
import useScreenError from '../hooks/useScreenError';
import client from '../api/client';
import { colors, fonts, radii, screenStyles, buttonStyles, modalStyles } from '../theme/styles';

export default function ShipScreen({ navigation, route }) {
  const scrollRef = React.useRef(null);
  useScrollToTop(scrollRef);
  const [order, setOrder] = useState(null);
  const [lines, setLines] = useState([]);
  const [totalItems, setTotalItems] = useState(0);
  const [phase, setPhase] = useState('scan_order'); // scan_order | shipping | done
  const [carrier, setCarrier] = useState('');
  const [isCustomCarrier, setIsCustomCarrier] = useState(false);
  const [showCarrierPicker, setShowCarrierPicker] = useState(false);
  const [tracking, setTracking] = useState('');
  const { error, scanDisabled, showError, clearError } = useScreenError();
  const [showSODetail, setShowSODetail] = useState(false);
  const [soDetail, setSODetail] = useState(null);

  const CARRIERS = ['UPS', 'FedEx', 'USPS', 'DHL', 'Amazon', 'Other'];

  // Auto-load SO if navigated from home screen scan
  useEffect(() => {
    const soNumber = route?.params?.so_number;
    if (soNumber) {
      handleScanOrder(soNumber);
    }
  }, []);

  const handleScanOrder = async (barcode) => {
    try {
      const resp = await client.get(`/api/shipping/order/${encodeURIComponent(barcode)}`);
      const data = resp.data;
      setOrder(data.sales_order);
      setLines(data.lines || []);
      setTotalItems(data.total_items || 0);
      setPhase('shipping');
    } catch (err) {
      showError(err.response?.data?.error || 'Order not found');
    }
  };

  const handleShip = async () => {
    if (!carrier.trim() || !tracking.trim()) {
      showError('Carrier and tracking number are required');
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
      showError(err.response?.data?.error || 'Shipment failed');
    }
  };

  const showOrderDetail = async () => {
    if (!order) return;
    try {
      const resp = await client.get(`/api/lookup/so/${encodeURIComponent(order.so_number)}`);
      setSODetail(resp.data.sales_order);
    } catch {
      setSODetail({ so_number: order.so_number, customer_name: order.customer_name });
    }
    setShowSODetail(true);
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
    <View style={screenStyles.screen}>
      <ScreenHeader title="SHIP" onBack={() => navigation.goBack()} />

      <ScrollView ref={scrollRef} style={screenStyles.content} contentContainerStyle={screenStyles.contentInner} keyboardShouldPersistTaps="handled">
        {phase === 'scan_order' && (
          <ScanInput placeholder="SCAN ORDER" onScan={handleScanOrder} disabled={scanDisabled} />
        )}

        {phase === 'shipping' && (
          <>
            <TouchableOpacity style={styles.orderInfo} onPress={showOrderDetail} activeOpacity={0.7}>
              <Text style={styles.soNumber}>{order.so_number}</Text>
              <Text style={styles.customer}>{order.customer_name}</Text>
              <Text style={styles.statusLabel}>
                {order.status === 'PACKED' ? 'PACKED - READY TO SHIP' : 'READY TO SHIP'}
              </Text>
              <Text style={styles.tapHint}>Tap for details</Text>
            </TouchableOpacity>

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
              <Text style={[styles.pickerText, !carrier && { color: colors.textPlaceholder }]}>
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
                placeholderTextColor={colors.textPlaceholder}
                autoFocus
              />
            )}

            <Text style={styles.fieldLabel}>TRACKING NUMBER</Text>
            <TextInput
              style={styles.textInput}
              value={tracking}
              onChangeText={setTracking}
              placeholder="Enter tracking number"
              placeholderTextColor={colors.textPlaceholder}
              autoCapitalize="characters"
            />

            <TouchableOpacity style={[buttonStyles.buttonPrimary, { marginTop: 16, width: '100%' }]} onPress={handleShip}>
              <Text style={buttonStyles.buttonPrimaryText}>SHIP</Text>
            </TouchableOpacity>
          </>
        )}

        {phase === 'done' && (
          <View style={styles.doneContainer}>
            <Text style={styles.doneIcon}>&#10003;</Text>
            <Text style={styles.doneTitle}>Order {order.so_number} shipped!</Text>
            <Text style={styles.doneDetail}>{carrier} - {tracking}</Text>
            <TouchableOpacity style={[buttonStyles.buttonPrimary, { marginTop: 16, width: '100%' }]} onPress={resetScreen}>
              <Text style={buttonStyles.buttonPrimaryText}>SHIP ANOTHER ORDER</Text>
            </TouchableOpacity>
            <TouchableOpacity style={[buttonStyles.buttonSecondary, { marginTop: 8, width: '100%' }]} onPress={() => navigation.goBack()}>
              <Text style={[buttonStyles.buttonSecondaryText, { fontWeight: '700' }]}>DONE</Text>
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

      {/* SO Detail Modal */}
      <Modal visible={showSODetail} transparent animationType="fade">
        <Pressable style={modalStyles.overlay} onPress={() => setShowSODetail(false)}>
          <View style={modalStyles.card}>
            <Text style={modalStyles.title}>ORDER DETAILS</Text>
            {soDetail && (
              <ScrollView style={{ maxHeight: 300 }}>
                <View style={styles.detailRow}><Text style={styles.detailLabel}>ORDER</Text><Text style={styles.detailValue}>{soDetail.so_number}</Text></View>
                <View style={styles.detailRow}><Text style={styles.detailLabel}>CUSTOMER</Text><Text style={styles.detailValue}>{soDetail.customer_name || '-'}</Text></View>
                {soDetail.customer_phone && <View style={styles.detailRow}><Text style={styles.detailLabel}>PHONE</Text><Text style={styles.detailValue}>{soDetail.customer_phone}</Text></View>}
                {(soDetail.customer_address || soDetail.ship_address) && <View style={styles.detailRow}><Text style={styles.detailLabel}>ADDRESS</Text><Text style={styles.detailValue}>{soDetail.customer_address || soDetail.ship_address}</Text></View>}
                <View style={styles.detailRow}><Text style={styles.detailLabel}>STATUS</Text><Text style={styles.detailValue}>{soDetail.status}</Text></View>
                {soDetail.lines?.length > 0 && (
                  <View style={{ marginTop: 12 }}>
                    <Text style={styles.detailLabel}>ITEMS</Text>
                    {soDetail.lines.map((l, i) => (
                      <View key={i} style={styles.detailItemRow}>
                        <Text style={styles.detailItemSku}>{l.sku}</Text>
                        <Text style={styles.detailItemQty}>{l.quantity_ordered}</Text>
                      </View>
                    ))}
                  </View>
                )}
              </ScrollView>
            )}
            <TouchableOpacity style={[buttonStyles.buttonSecondary, { marginTop: 16 }]} onPress={() => setShowSODetail(false)}>
              <Text style={buttonStyles.buttonSecondaryText}>CLOSE</Text>
            </TouchableOpacity>
          </View>
        </Pressable>
      </Modal>

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
  statusLabel: { fontFamily: fonts.mono, fontSize: 12, color: colors.success, letterSpacing: 0.3, marginTop: 4 },
  summaryRow: {
    flexDirection: 'row', gap: 12, marginBottom: 16,
  },
  summaryItem: {
    flex: 1, borderWidth: 1, borderColor: colors.cardBorder, borderRadius: radii.card,
    backgroundColor: colors.cardBg, padding: 12, alignItems: 'center',
  },
  summaryValue: { fontFamily: fonts.mono, fontSize: 20, fontWeight: '700', color: colors.textPrimary },
  summaryLabel: { fontFamily: fonts.mono, fontSize: 10, color: colors.textMuted, letterSpacing: 0.3, marginTop: 2 },
  fieldLabel: {
    fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted,
    letterSpacing: 0.3, marginBottom: 4, marginTop: 12,
  },
  textInput: {
    borderWidth: 1, borderColor: colors.inputBorder, borderRadius: radii.input,
    paddingHorizontal: 12, paddingVertical: 10, fontSize: 14,
    color: colors.textPrimary, backgroundColor: colors.inputBg, minHeight: 48, marginBottom: 8,
  },
  doneContainer: { alignItems: 'center', paddingTop: 40 },
  doneIcon: { fontSize: 48, color: colors.success, marginBottom: 16 },
  doneTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, marginBottom: 4 },
  doneDetail: { fontFamily: fonts.mono, fontSize: 13, color: colors.textMuted, marginBottom: 24 },
  pickerBtn: {
    borderWidth: 1, borderColor: colors.inputBorder, borderRadius: radii.input,
    paddingHorizontal: 12, paddingVertical: 12, minHeight: 48, marginBottom: 8,
    flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center',
    backgroundColor: colors.inputBg,
  },
  pickerText: { fontSize: 14, color: colors.textPrimary, fontFamily: fonts.mono },
  pickerOverlay: {
    flex: 1, backgroundColor: colors.overlay,
    justifyContent: 'center', alignItems: 'center', padding: 32,
  },
  pickerCard: {
    backgroundColor: colors.background, borderRadius: radii.card, padding: 20, width: '100%',
    borderWidth: 1, borderColor: colors.cardBorder,
  },
  pickerTitle: { fontFamily: fonts.mono, fontSize: 12, fontWeight: '700', color: colors.textMuted, letterSpacing: 0.5, marginBottom: 12 },
  pickerOption: {
    padding: 14, borderRadius: radii.card, borderWidth: 1, borderColor: colors.cardBorder, marginBottom: 8,
  },
  pickerOptionActive: { borderColor: colors.accentRed, backgroundColor: '#fdf6f4' },
  pickerOptionText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textPrimary },
  pickerOptionTextActive: { color: colors.accentRed },
  tapHint: { fontFamily: fonts.mono, fontSize: 10, color: colors.textPlaceholder, marginTop: 2 },
  detailRow: { flexDirection: 'row', justifyContent: 'space-between', paddingVertical: 6, borderBottomWidth: 1, borderBottomColor: colors.cardBorder },
  detailLabel: { fontFamily: fonts.mono, fontSize: 11, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3 },
  detailValue: { fontFamily: fonts.mono, fontSize: 13, color: colors.textPrimary, textAlign: 'right', flex: 1, marginLeft: 12 },
  detailItemRow: { flexDirection: 'row', justifyContent: 'space-between', paddingVertical: 4, paddingLeft: 8 },
  detailItemSku: { fontFamily: fonts.mono, fontSize: 12, color: colors.textPrimary },
  detailItemQty: { fontFamily: fonts.mono, fontSize: 12, fontWeight: '700', color: colors.accentRed },
});
