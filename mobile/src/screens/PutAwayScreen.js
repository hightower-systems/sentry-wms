import React, { useState, useEffect } from 'react';
import { View, Text, TouchableOpacity, ScrollView, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

export default function PutAwayScreen({ navigation }) {
  const { warehouseId } = useAuth();
  const [pendingItems, setPendingItems] = useState([]);
  const [selectedItem, setSelectedItem] = useState(null);
  const [suggestedBin, setSuggestedBin] = useState(null);
  const [scannedBin, setScannedBin] = useState(null);
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);

  const loadPending = async () => {
    try {
      const resp = await client.get(`/api/putaway/pending/${warehouseId}`);
      setPendingItems(resp.data.items || resp.data || []);
    } catch {
      // silent
    }
  };

  useEffect(() => {
    loadPending();
  }, [warehouseId]);

  const handleSelectItem = async (item) => {
    setSelectedItem(item);
    setScannedBin(null);
    try {
      const resp = await client.get(`/api/putaway/suggest/${item.item_id}`);
      setSuggestedBin(resp.data);
    } catch {
      setSuggestedBin(null);
    }
  };

  const handleScanBin = async (barcode) => {
    try {
      const resp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
      if (resp.data && resp.data.bin) {
        setScannedBin(resp.data.bin);
      } else {
        setError('Bin not found');
        setScanDisabled(true);
      }
    } catch {
      setError('Bin not found');
      setScanDisabled(true);
    }
  };

  const handleConfirm = async () => {
    if (!selectedItem || !scannedBin) return;
    try {
      await client.post('/api/putaway/confirm', {
        item_id: selectedItem.item_id,
        from_bin_id: selectedItem.bin_id,
        to_bin_id: scannedBin.bin_id,
        quantity: selectedItem.quantity_on_hand || selectedItem.quantity,
        warehouse_id: warehouseId,
      });
      setSelectedItem(null);
      setSuggestedBin(null);
      setScannedBin(null);
      await loadPending();
    } catch (err) {
      setError(err.response?.data?.error || 'Put-away failed');
      setScanDisabled(true);
    }
  };

  const allDone = pendingItems.length === 0 && !selectedItem;

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>PUT-AWAY</Text>
        <View style={{ width: 32 }} />
      </View>

      <ScrollView style={styles.content} contentContainerStyle={styles.contentInner}>
        {selectedItem ? (
          <>
            {suggestedBin && (
              <View style={styles.binCard}>
                <Text style={styles.binLabel}>SUGGESTED BIN</Text>
                <Text style={styles.binCode}>{suggestedBin.bin_code}</Text>
                {suggestedBin.zone_name && (
                  <Text style={styles.binZone}>
                    {suggestedBin.zone_name}
                    {suggestedBin.aisle ? ` · AISLE ${suggestedBin.aisle}` : ''}
                  </Text>
                )}
              </View>
            )}

            <View style={styles.itemDetail}>
              <Text style={styles.sku}>{selectedItem.sku}</Text>
              <Text style={styles.itemName}>{selectedItem.item_name}</Text>
              <Text style={styles.qty}>QTY: {selectedItem.quantity_on_hand || selectedItem.quantity}</Text>
            </View>

            <ScanInput
              placeholder="SCAN DESTINATION BIN"
              onScan={handleScanBin}
              disabled={scanDisabled || !!scannedBin}
            />

            {scannedBin && (
              <>
                <View style={styles.confirmedBin}>
                  <Text style={styles.label}>DESTINATION</Text>
                  <Text style={styles.confirmedBinCode}>{scannedBin.bin_code}</Text>
                </View>
                <TouchableOpacity style={styles.buttonPrimary} onPress={handleConfirm}>
                  <Text style={styles.buttonPrimaryText}>CONFIRM</Text>
                </TouchableOpacity>
              </>
            )}

            <TouchableOpacity
              style={styles.buttonSecondary}
              onPress={() => {
                setSelectedItem(null);
                setSuggestedBin(null);
                setScannedBin(null);
              }}
            >
              <Text style={styles.buttonSecondaryText}>BACK TO LIST</Text>
            </TouchableOpacity>
          </>
        ) : allDone ? (
          <View style={styles.emptyState}>
            <Text style={styles.emptyText}>All items put away</Text>
          </View>
        ) : (
          pendingItems.map((item, index) => (
            <TouchableOpacity
              key={item.item_id || index}
              style={styles.listItem}
              onPress={() => handleSelectItem(item)}
            >
              <View style={{ flex: 1 }}>
                <Text style={styles.sku}>{item.sku}</Text>
                <Text style={styles.itemName}>{item.item_name}</Text>
              </View>
              <View style={styles.stagingBadge}>
                <Text style={styles.stagingQty}>{item.quantity_on_hand || item.quantity}</Text>
                <Text style={styles.stagingLabel}>in staging</Text>
              </View>
            </TouchableOpacity>
          ))
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
  binCard: {
    borderWidth: 1.5, borderColor: colors.accentRed, borderRadius: 8,
    padding: 20, marginBottom: 16, alignItems: 'center',
  },
  binLabel: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3, marginBottom: 4 },
  binCode: { fontFamily: fonts.mono, fontSize: 30, fontWeight: '700', color: colors.accentRed },
  binZone: { fontFamily: fonts.mono, fontSize: 12, color: colors.copper, letterSpacing: 0.3, marginTop: 4, textTransform: 'uppercase' },
  itemDetail: { marginBottom: 16 },
  sku: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textPrimary },
  itemName: { fontSize: 13, color: colors.textMuted, marginTop: 2 },
  qty: { fontFamily: fonts.mono, fontSize: 14, color: colors.textPrimary, marginTop: 4 },
  label: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3 },
  confirmedBin: { marginBottom: 16 },
  confirmedBinCode: { fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary },
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48, marginBottom: 12,
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  buttonSecondary: {
    backgroundColor: colors.background, borderWidth: 1.5, borderColor: colors.border, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonSecondaryText: { color: colors.textMuted, fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', letterSpacing: 0.5 },
  listItem: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    padding: 14, marginBottom: 8, minHeight: 48,
  },
  stagingBadge: { alignItems: 'flex-end' },
  stagingQty: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary },
  stagingLabel: { fontSize: 10, color: colors.textMuted },
  emptyState: { alignItems: 'center', paddingVertical: 48 },
  emptyText: { fontFamily: fonts.mono, fontSize: 14, color: colors.textMuted },
});
