import React, { useState, useEffect } from 'react';
import { useScrollToTop } from '@react-navigation/native';
import { View, Text, TouchableOpacity, ScrollView, Modal, Pressable, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ScreenHeader from '../components/ScreenHeader';
import ErrorPopup from '../components/ErrorPopup';
import useScreenError from '../hooks/useScreenError';
import client from '../api/client';
import { colors, fonts, radii, screenStyles, buttonStyles, listStyles, modalStyles } from '../theme/styles';

export default function PackScreen({ navigation, route }) {
  const scrollRef = React.useRef(null);
  useScrollToTop(scrollRef);
  const [order, setOrder] = useState(null);
  const [items, setItems] = useState([]);
  const [phase, setPhase] = useState('scan_order'); // scan_order | packing | done
  const { error, scanDisabled, showError, clearError } = useScreenError();
  const [showSODetail, setShowSODetail] = useState(false);
  const [soDetail, setSODetail] = useState(null);

  // Auto-load SO if navigated from home screen scan
  useEffect(() => {
    const soNumber = route?.params?.so_number;
    if (soNumber) {
      handleScanOrder(soNumber);
    }
  }, []);

  const handleScanOrder = async (barcode) => {
    try {
      const resp = await client.get(`/api/packing/order/${encodeURIComponent(barcode)}`);
      const data = resp.data;
      setOrder(data.sales_order || data.order || data);
      setItems(
        (data.lines || data.items || []).map((item) => ({
          ...item,
          verified: item.quantity_packed || item.quantity_verified || 0,
        }))
      );
      setPhase('packing');
    } catch (err) {
      showError(err.response?.data?.error || 'Order not found');
    }
  };

  const handleScanItem = async (barcode) => {
    // Client-side check: find the matching item and verify it's not already complete
    const matchedItem = items.find(
      (item) => item.sku === barcode || item.upc === barcode || item.item_barcode === barcode
    );
    if (matchedItem) {
      const expected = matchedItem.quantity_picked || matchedItem.quantity_ordered;
      if ((matchedItem.verified || 0) >= expected) {
        showError(`${matchedItem.sku} already fully verified`);
        return;
      }
    }

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
    setItems([]);
    setPhase('scan_order');
  };

  return (
    <View style={screenStyles.screen}>
      <ScreenHeader title="PACK" onBack={() => navigation.goBack()} />

      <ScrollView ref={scrollRef} style={screenStyles.content} contentContainerStyle={screenStyles.contentInner} keyboardShouldPersistTaps="handled">
        {phase === 'scan_order' && (
          <ScanInput placeholder="SCAN ORDER" onScan={handleScanOrder} disabled={scanDisabled} />
        )}

        {phase === 'packing' && (
          <>
            <TouchableOpacity style={styles.orderInfo} onPress={showOrderDetail} activeOpacity={0.7}>
              <Text style={styles.soNumber}>{order.so_number}</Text>
              <Text style={styles.customer}>{order.customer_name}</Text>
              <Text style={styles.tapHint}>Tap for details</Text>
            </TouchableOpacity>

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
                    {complete ? (
                      <Text style={styles.checkIcon}>&#10003;</Text>
                    ) : (
                      <TouchableOpacity
                        style={styles.manualPackBtn}
                        onPress={() => handleScanItem(item.upc || item.sku)}
                      >
                        <Text style={styles.manualPackText}>PACK</Text>
                      </TouchableOpacity>
                    )}
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
  itemRowComplete: { borderColor: colors.success, backgroundColor: '#f0f9f0' },
  itemQty: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  itemQtyText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  itemQtyComplete: { color: colors.success },
  checkIcon: { fontSize: 16, color: colors.success },
  doneContainer: { alignItems: 'center', paddingTop: 40 },
  doneIcon: { fontSize: 48, color: colors.success, marginBottom: 16 },
  doneTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, marginBottom: 24 },
  tapHint: { fontFamily: fonts.mono, fontSize: 10, color: colors.textPlaceholder, marginTop: 2 },
  detailRow: { flexDirection: 'row', justifyContent: 'space-between', paddingVertical: 6, borderBottomWidth: 1, borderBottomColor: colors.cardBorder },
  detailLabel: { fontFamily: fonts.mono, fontSize: 11, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3 },
  detailValue: { fontFamily: fonts.mono, fontSize: 13, color: colors.textPrimary, textAlign: 'right', flex: 1, marginLeft: 12 },
  detailItemRow: { flexDirection: 'row', justifyContent: 'space-between', paddingVertical: 4, paddingLeft: 8 },
  detailItemSku: { fontFamily: fonts.mono, fontSize: 12, color: colors.textPrimary },
  detailItemQty: { fontFamily: fonts.mono, fontSize: 12, fontWeight: '700', color: colors.accentRed },
  manualPackBtn: {
    backgroundColor: colors.accentRed, borderRadius: radii.badge,
    paddingHorizontal: 10, paddingVertical: 4, minHeight: 28,
    alignItems: 'center', justifyContent: 'center',
  },
  manualPackText: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '700', color: colors.cream, letterSpacing: 0.5 },
});
