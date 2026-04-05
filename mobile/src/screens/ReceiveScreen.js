import React, { useState } from 'react';
import { View, Text, TouchableOpacity, ScrollView, TextInput, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

export default function ReceiveScreen({ navigation }) {
  const { warehouseId } = useAuth();
  const [po, setPo] = useState(null);
  const [lines, setLines] = useState([]);
  const [activeItem, setActiveItem] = useState(null);
  const [quantity, setQuantity] = useState('');
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);
  const [poComplete, setPoComplete] = useState(false);

  const handleScanPO = async (barcode) => {
    try {
      const resp = await client.get(`/api/receiving/po/${encodeURIComponent(barcode)}`);
      setPo(resp.data.po || resp.data);
      setLines(resp.data.lines || []);
      setActiveItem(null);
      setPoComplete(false);
    } catch (err) {
      setError(err.response?.data?.error || 'PO not found');
      setScanDisabled(true);
    }
  };

  const handleScanItem = async (barcode) => {
    const match = lines.find(
      (l) => l.upc === barcode || l.sku === barcode || l.item_barcode === barcode
    );
    if (!match) {
      setError('Item not on this PO');
      setScanDisabled(true);
      return;
    }
    const remaining = match.quantity_ordered - match.quantity_received;
    setActiveItem(match);
    setQuantity(String(remaining > 0 ? remaining : 1));
  };

  const handleConfirm = async () => {
    if (!activeItem) return;
    const qty = parseInt(quantity, 10);
    if (!qty || qty <= 0) return;

    const remaining = activeItem.quantity_ordered - activeItem.quantity_received;

    try {
      await client.post('/api/receiving/receive', {
        po_id: po.po_id,
        items: [{
          item_id: activeItem.item_id,
          quantity: qty,
          bin_id: activeItem.staging_bin_id || 1,
        }],
        warehouse_id: warehouseId,
      });

      // Refresh PO data
      const resp = await client.get(`/api/receiving/po/${encodeURIComponent(po.po_barcode || po.po_number)}`);
      const updatedLines = resp.data.lines || [];
      setLines(updatedLines);
      setPo(resp.data.po || resp.data);
      setActiveItem(null);
      setQuantity('');

      if (qty > remaining && remaining > 0) {
        setError(`Receiving ${qty - remaining} over expected quantity`);
        setScanDisabled(true);
      }

      const allDone = updatedLines.every((l) => l.quantity_received >= l.quantity_ordered);
      if (allDone) setPoComplete(true);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to receive');
      setScanDisabled(true);
    }
  };

  const resetPO = () => {
    setPo(null);
    setLines([]);
    setActiveItem(null);
    setPoComplete(false);
  };

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>RECEIVE</Text>
        <View style={{ width: 32 }} />
      </View>

      <ScrollView style={styles.content} contentContainerStyle={styles.contentInner}>
        {!po ? (
          <ScanInput placeholder="SCAN PO" onScan={handleScanPO} disabled={scanDisabled} />
        ) : poComplete ? (
          <View style={styles.completeCard}>
            <Text style={styles.completeText}>PO Complete</Text>
            <Text style={styles.completeDetail}>{po.po_number} — all items received</Text>
            <TouchableOpacity style={styles.buttonPrimary} onPress={resetPO}>
              <Text style={styles.buttonPrimaryText}>SCAN ANOTHER PO</Text>
            </TouchableOpacity>
          </View>
        ) : (
          <>
            <View style={styles.poHeader}>
              <Text style={styles.poNumber}>{po.po_number}</Text>
              <Text style={styles.poVendor}>{po.vendor_name}</Text>
            </View>

            <ScanInput
              placeholder="SCAN ITEM"
              onScan={handleScanItem}
              disabled={scanDisabled || !!activeItem}
            />

            {activeItem && (
              <View style={styles.receiveCard}>
                <Text style={styles.sku}>{activeItem.sku}</Text>
                <Text style={styles.itemName}>{activeItem.item_name}</Text>
                <View style={styles.qtyRow}>
                  <Text style={styles.label}>QUANTITY</Text>
                  <TextInput
                    style={styles.qtyInput}
                    value={quantity}
                    onChangeText={setQuantity}
                    keyboardType="number-pad"
                  />
                </View>
                <TouchableOpacity style={styles.buttonPrimary} onPress={handleConfirm}>
                  <Text style={styles.buttonPrimaryText}>CONFIRM</Text>
                </TouchableOpacity>
              </View>
            )}

            {lines.map((line) => (
              <View key={line.po_line_id || line.item_id} style={styles.lineRow}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.sku}>{line.sku}</Text>
                  <Text style={styles.itemName}>{line.item_name}</Text>
                </View>
                <Text style={styles.lineQty}>
                  {line.quantity_received}/{line.quantity_ordered}
                </Text>
              </View>
            ))}
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
    borderBottomWidth: 2, borderBottomColor: colors.accentRed,
  },
  backBtn: { padding: 4, minWidth: 32, minHeight: 48, justifyContent: 'center' },
  backText: { fontSize: 22, color: colors.textPrimary },
  headerTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, letterSpacing: 0.5 },
  content: { flex: 1 },
  contentInner: { padding: 16 },
  poHeader: { marginBottom: 16 },
  poNumber: { fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary },
  poVendor: { fontSize: 13, color: colors.textMuted, marginTop: 2 },
  receiveCard: {
    borderWidth: 1.5, borderColor: colors.accentRed, borderRadius: 8,
    padding: 16, marginBottom: 16,
  },
  sku: { fontFamily: fonts.mono, fontSize: 14, color: colors.textPrimary, fontWeight: '600' },
  itemName: { fontSize: 13, color: colors.textMuted, marginTop: 2 },
  qtyRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginVertical: 12 },
  label: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3 },
  qtyInput: {
    fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary,
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    paddingHorizontal: 12, paddingVertical: 8, width: 80, textAlign: 'center', minHeight: 48,
  },
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  lineRow: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    padding: 12, marginBottom: 8, minHeight: 48,
  },
  lineQty: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  completeCard: { alignItems: 'center', paddingVertical: 32 },
  completeText: { fontFamily: fonts.mono, fontSize: 20, fontWeight: '700', color: colors.success, marginBottom: 4 },
  completeDetail: { fontFamily: fonts.mono, fontSize: 13, color: colors.textMuted, marginBottom: 24 },
});
