import React, { useState } from 'react';
import { View, Text, TouchableOpacity, ScrollView, Modal, Alert, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

export default function PutAwayScreen({ navigation }) {
  const { warehouseId } = useAuth();

  // Current item being put away
  const [item, setItem] = useState(null);
  const [preferredBin, setPreferredBin] = useState(null);
  const [fromBinId, setFromBinId] = useState(null);
  const [quantity, setQuantity] = useState(0);
  const [lotNumber, setLotNumber] = useState(null);

  // Session history
  const [history, setHistory] = useState([]);

  // Preferred bin prompt
  const [showPreferredPrompt, setShowPreferredPrompt] = useState(false);
  const [promptData, setPromptData] = useState(null);

  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);
  const [phase, setPhase] = useState('scan_item'); // scan_item | scan_bin

  // Step 1: Scan item barcode
  const handleScanItem = async (barcode) => {
    try {
      // Look up item
      const itemResp = await client.get(`/api/lookup/item/${encodeURIComponent(barcode)}`);
      if (!itemResp.data?.item) {
        setError('Item not found');
        setScanDisabled(true);
        return;
      }

      const scannedItem = itemResp.data.item;

      // Check if this item is in a staging/receiving bin
      const locations = itemResp.data.locations || [];
      const stagingLoc = locations.find(
        (l) => l.bin_type === 'RECEIVING' || l.bin_type === 'INBOUND_STAGING'
      );

      if (!stagingLoc) {
        setError('Item not in a staging bin');
        setScanDisabled(true);
        return;
      }

      setFromBinId(stagingLoc.bin_id);
      setQuantity(stagingLoc.quantity_on_hand);
      setLotNumber(stagingLoc.lot_number || null);

      // Get preferred bin suggestion
      const suggestResp = await client.get(`/api/putaway/suggest/${scannedItem.item_id}`);
      const preferred = suggestResp.data.preferred_bin || suggestResp.data.suggested_bin || null;

      setItem(scannedItem);
      setPreferredBin(preferred);
      setPhase('scan_bin');
    } catch {
      setError('Item not found');
      setScanDisabled(true);
    }
  };

  // Step 3: Scan bin to confirm put-away
  const handleScanBin = async (barcode) => {
    try {
      const binResp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
      if (!binResp.data?.bin) {
        setError('Bin not found');
        setScanDisabled(true);
        return;
      }

      const scannedBin = binResp.data.bin;

      // Execute put-away
      await client.post('/api/putaway/confirm', {
        item_id: item.item_id,
        from_bin_id: fromBinId,
        to_bin_id: scannedBin.bin_id,
        quantity: quantity,
        lot_number: lotNumber,
        warehouse_id: warehouseId,
      });

      // Add to session history
      setHistory((prev) => [...prev, {
        sku: item.sku,
        item_name: item.item_name,
        bin_code: scannedBin.bin_code,
        quantity: quantity,
      }]);

      // Determine if we need to show the preferred bin prompt
      const matchesPreferred = preferredBin && scannedBin.bin_id === preferredBin.bin_id;

      if (matchesPreferred) {
        // Matches preferred - just reset, no prompt needed
        resetForNextItem();
      } else if (!preferredBin) {
        // No preferred bin exists - offer to set one
        setPromptData({
          type: 'set_new',
          item,
          newBin: scannedBin,
          oldBin: null,
        });
        setShowPreferredPrompt(true);
      } else {
        // Different bin from preferred - offer to change
        setPromptData({
          type: 'change',
          item,
          newBin: scannedBin,
          oldBin: preferredBin,
        });
        setShowPreferredPrompt(true);
      }
    } catch (err) {
      setError(err.response?.data?.error || 'Put-away failed');
      setScanDisabled(true);
    }
  };

  const handleUpdatePreferred = async () => {
    if (!promptData) return;
    try {
      await client.post('/api/putaway/update-preferred', {
        item_id: promptData.item.item_id,
        bin_id: promptData.newBin.bin_id,
        set_as_primary: true,
      });
    } catch {
      // Silent - non-critical
    }
    setShowPreferredPrompt(false);
    setPromptData(null);
    resetForNextItem();
  };

  const handleSkipPreferred = () => {
    setShowPreferredPrompt(false);
    setPromptData(null);
    resetForNextItem();
  };

  const resetForNextItem = () => {
    setItem(null);
    setPreferredBin(null);
    setFromBinId(null);
    setQuantity(0);
    setLotNumber(null);
    setPhase('scan_item');
  };

  const handleCancel = () => {
    if (history.length === 0) {
      navigation.goBack();
      return;
    }
    Alert.alert(
      'Leave Put-Away',
      'Are you sure? All put-aways in this session have already been saved.',
      [
        { text: 'Stay', style: 'cancel' },
        { text: 'Leave', onPress: () => navigation.goBack() },
      ]
    );
  };

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>PUT-AWAY</Text>
        {history.length > 0 ? (
          <View style={styles.badge}>
            <Text style={styles.badgeText}>{history.length}</Text>
          </View>
        ) : (
          <View style={{ width: 32 }} />
        )}
      </View>

      <ScrollView style={styles.content} contentContainerStyle={styles.contentInner} keyboardShouldPersistTaps="handled">
        {phase === 'scan_item' && (
          <>
            <ScanInput placeholder="SCAN ITEM" onScan={handleScanItem} disabled={scanDisabled} />

            {history.length > 0 && (
              <View style={styles.historySection}>
                <Text style={styles.historyTitle}>
                  Put-aways this session: {history.length}
                </Text>
                {history.map((h, i) => (
                  <View key={i} style={styles.historyRow}>
                    <Text style={styles.historyCheck}>{'\u2713'}</Text>
                    <View style={{ flex: 1 }}>
                      <Text style={styles.historySku}>{h.sku}</Text>
                      <Text style={styles.historyDetail}>{h.item_name}</Text>
                    </View>
                    <Text style={styles.historyBin}>{'\u2192'} {h.bin_code}</Text>
                  </View>
                ))}
              </View>
            )}
          </>
        )}

        {phase === 'scan_bin' && item && (
          <>
            {/* Item info */}
            <View style={styles.itemCard}>
              <Text style={styles.itemName}>{item.item_name}</Text>
              <Text style={styles.sku}>{item.sku}</Text>
              <Text style={styles.qty}>QTY: {quantity}</Text>
            </View>

            {/* Suggested bin */}
            {preferredBin ? (
              <View style={styles.suggestCard}>
                <Text style={styles.suggestLabel}>SUGGESTED BIN</Text>
                <Text style={styles.suggestBinCode}>{preferredBin.bin_code}</Text>
                {preferredBin.zone_name && (
                  <Text style={styles.suggestZone}>{preferredBin.zone_name}</Text>
                )}
              </View>
            ) : (
              <View style={styles.noPreferredCard}>
                <Text style={styles.noPreferredText}>No preferred bin set.</Text>
                <Text style={styles.noPreferredSub}>Scan any bin to put away.</Text>
              </View>
            )}

            <ScanInput placeholder="SCAN BIN TO CONFIRM" onScan={handleScanBin} disabled={scanDisabled} />
          </>
        )}
      </ScrollView>

      {/* Bottom bar */}
      <View style={styles.bottomBar}>
        <TouchableOpacity style={styles.buttonDone} onPress={() => navigation.goBack()}>
          <Text style={styles.buttonDoneText}>DONE</Text>
        </TouchableOpacity>
        {phase === 'scan_bin' && (
          <TouchableOpacity style={styles.buttonCancel} onPress={resetForNextItem}>
            <Text style={styles.buttonCancelText}>BACK</Text>
          </TouchableOpacity>
        )}
      </View>

      {/* Preferred bin prompt modal */}
      <Modal visible={showPreferredPrompt} transparent animationType="fade">
        <View style={styles.modalOverlay}>
          <View style={styles.modalCard}>
            {promptData?.type === 'set_new' ? (
              <>
                <Text style={styles.modalTitle}>Set preferred bin for</Text>
                <Text style={styles.modalItemName}>{promptData.item.item_name}</Text>
                <Text style={styles.modalSku}>{promptData.item.sku}</Text>
                <View style={styles.modalDivider} />
                <Text style={styles.modalBody}>
                  Set {promptData.newBin.bin_code} as preferred bin?
                </Text>
              </>
            ) : (
              <>
                <Text style={styles.modalTitle}>Set preferred bin for</Text>
                <Text style={styles.modalItemName}>{promptData?.item?.item_name}</Text>
                <Text style={styles.modalSku}>{promptData?.item?.sku}</Text>
                <View style={styles.modalDivider} />
                <Text style={styles.modalBody}>
                  Change preferred bin from {promptData?.oldBin?.bin_code} to {promptData?.newBin?.bin_code}?
                </Text>
              </>
            )}

            <View style={styles.modalActions}>
              <TouchableOpacity style={styles.buttonPrimary} onPress={handleUpdatePreferred}>
                <Text style={styles.buttonPrimaryText}>
                  {promptData?.type === 'set_new' ? 'YES, SET' : 'YES, UPDATE'}
                </Text>
              </TouchableOpacity>
              <TouchableOpacity style={styles.buttonSecondary} onPress={handleSkipPreferred}>
                <Text style={styles.buttonSecondaryText}>
                  {promptData?.type === 'set_new' ? 'NO, SKIP' : 'NO, KEEP CURRENT'}
                </Text>
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
  backBtn: { padding: 4, minWidth: 32, minHeight: 48, justifyContent: 'center' },
  backText: { fontSize: 22, color: colors.textPrimary },
  headerTitle: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary, letterSpacing: 0.5 },
  badge: {
    backgroundColor: colors.accentRed, borderRadius: 10,
    paddingHorizontal: 8, paddingVertical: 2, minWidth: 24, alignItems: 'center',
  },
  badgeText: { color: '#FFFFFF', fontFamily: fonts.mono, fontSize: 12, fontWeight: '700' },
  content: { flex: 1 },
  contentInner: { padding: 16 },

  // Item card
  itemCard: { marginBottom: 16 },
  itemName: { fontSize: 16, fontWeight: '600', color: colors.textPrimary },
  sku: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textMuted, marginTop: 2 },
  qty: { fontFamily: fonts.mono, fontSize: 14, color: colors.textPrimary, marginTop: 4 },

  // Suggested bin
  suggestCard: {
    borderWidth: 1.5, borderColor: colors.accentRed, borderRadius: 8,
    padding: 20, marginBottom: 16, alignItems: 'center',
  },
  suggestLabel: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3, marginBottom: 4 },
  suggestBinCode: { fontFamily: fonts.mono, fontSize: 30, fontWeight: '700', color: colors.accentRed },
  suggestZone: { fontFamily: fonts.mono, fontSize: 12, color: colors.copper, letterSpacing: 0.3, marginTop: 4, textTransform: 'uppercase' },

  // No preferred bin
  noPreferredCard: {
    borderWidth: 1, borderColor: colors.border, borderRadius: 8, borderStyle: 'dashed',
    padding: 20, marginBottom: 16, alignItems: 'center',
  },
  noPreferredText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textMuted },
  noPreferredSub: { fontSize: 13, color: colors.textMuted, marginTop: 4 },

  // History
  historySection: { marginTop: 16 },
  historyTitle: { fontFamily: fonts.mono, fontSize: 12, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3, marginBottom: 8 },
  historyRow: {
    flexDirection: 'row', alignItems: 'center',
    borderWidth: 1, borderColor: colors.success, borderRadius: 8,
    padding: 12, marginBottom: 6, minHeight: 48,
  },
  historyCheck: { fontSize: 16, color: colors.success, marginRight: 10 },
  historySku: { fontFamily: fonts.mono, fontSize: 13, fontWeight: '600', color: colors.textPrimary },
  historyDetail: { fontSize: 12, color: colors.textMuted, marginTop: 1 },
  historyBin: { fontFamily: fonts.mono, fontSize: 13, fontWeight: '700', color: colors.textPrimary },

  // Bottom bar
  bottomBar: { padding: 16, borderTopWidth: 1, borderTopColor: colors.border, gap: 8 },
  buttonDone: {
    backgroundColor: colors.accentRed, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonDoneText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  buttonCancel: {
    backgroundColor: colors.background, borderWidth: 1.5, borderColor: colors.border, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonCancelText: { color: colors.textMuted, fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', letterSpacing: 0.5 },

  // Modal
  modalOverlay: { flex: 1, backgroundColor: 'rgba(0,0,0,0.4)', justifyContent: 'center', alignItems: 'center', padding: 32 },
  modalCard: { backgroundColor: colors.background, borderRadius: 8, padding: 24, width: '100%', maxWidth: 320 },
  modalTitle: { fontFamily: fonts.mono, fontSize: 12, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3 },
  modalItemName: { fontSize: 16, fontWeight: '600', color: colors.textPrimary, marginTop: 4 },
  modalSku: { fontFamily: fonts.mono, fontSize: 14, color: colors.textMuted, marginTop: 2 },
  modalDivider: { height: 1, backgroundColor: colors.border, marginVertical: 16 },
  modalBody: { fontSize: 14, color: colors.textPrimary, marginBottom: 20 },
  modalActions: { gap: 8 },
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
});
