import React, { useState } from 'react';
import { View, Text, TouchableOpacity, ScrollView, TextInput, Modal, Alert, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import PagedList from '../components/PagedList';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

export default function PutAwayScreen({ navigation }) {
  const { warehouseId } = useAuth();

  // Phase: 'load' → 'process' → 'done'
  const [phase, setPhase] = useState('load');

  // Load phase: queue of items to put away
  const [queue, setQueue] = useState([]);
  const [scanDisabled, setScanDisabled] = useState(false);

  // Process phase: working through queue
  const [currentIndex, setCurrentIndex] = useState(0);
  const [activeItem, setActiveItem] = useState(null);
  const [preferredBin, setPreferredBin] = useState(null);
  const [scannedBin, setScannedBin] = useState(null);
  const [putQty, setPutQty] = useState('');
  const [processPhase, setProcessPhase] = useState('scan_bin'); // scan_bin | enter_qty

  // Preferred bin prompt
  const [showPreferredPrompt, setShowPreferredPrompt] = useState(false);
  const [promptData, setPromptData] = useState(null);

  // Session history
  const [history, setHistory] = useState([]);
  const [error, setError] = useState('');

  // --- Load Phase ---

  const handleScanItem = async (barcode) => {
    // Check for staging bin scan first
    try {
      const binResp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
      if (binResp.data?.bin) {
        const bin = binResp.data.bin;
        if (bin.bin_type === 'Staging') {
          // Load all items from this staging bin
          const items = binResp.data.items || [];
          if (items.length === 0) {
            setError('No items in this staging bin');
            setScanDisabled(true);
            return;
          }
          const newEntries = items
            .filter((it) => !queue.find((q) => q.item_id === it.item_id && q.from_bin_id === bin.bin_id))
            .map((it) => ({
              item_id: it.item_id,
              sku: it.sku,
              item_name: it.item_name,
              upc: it.upc,
              from_bin_id: bin.bin_id,
              from_bin_code: bin.bin_code,
              quantity: it.quantity_on_hand,
              lot_number: it.lot_number || null,
            }));
          if (newEntries.length === 0) {
            setError('All items from this bin already loaded');
            setScanDisabled(true);
            return;
          }
          setQueue((prev) => [...prev, ...newEntries]);
          return;
        }
      }
    } catch {
      // Not a bin, try as item
    }

    // Item scan
    try {
      const itemResp = await client.get(`/api/lookup/item/${encodeURIComponent(barcode)}`);
      if (!itemResp.data?.item) {
        setError('Item not found');
        setScanDisabled(true);
        return;
      }

      const scannedItem = itemResp.data.item;
      const locations = itemResp.data.locations || [];
      const stagingLoc = locations.find(
        (l) => l.bin_type === 'Staging'
      );

      if (!stagingLoc) {
        setError('Item not in a staging bin');
        setScanDisabled(true);
        return;
      }

      // Duplicate check
      if (queue.find((q) => q.item_id === scannedItem.item_id && q.from_bin_id === stagingLoc.bin_id)) {
        setError('Already added');
        setScanDisabled(true);
        return;
      }

      setQueue((prev) => [...prev, {
        item_id: scannedItem.item_id,
        sku: scannedItem.sku,
        item_name: scannedItem.item_name,
        upc: scannedItem.upc,
        from_bin_id: stagingLoc.bin_id,
        from_bin_code: stagingLoc.bin_code,
        quantity: stagingLoc.quantity_on_hand,
        lot_number: stagingLoc.lot_number || null,
      }]);
    } catch {
      setError('Item not found');
      setScanDisabled(true);
    }
  };

  const removeFromQueue = (index) => {
    setQueue((prev) => prev.filter((_, i) => i !== index));
  };

  const handleLoadAll = async () => {
    if (queue.length === 0) return;
    setCurrentIndex(0);
    await loadItem(0);
  };

  // --- Process Phase ---

  const loadItem = async (index) => {
    if (index >= queue.length) {
      setPhase('done');
      return;
    }
    const entry = queue[index];
    setActiveItem(entry);
    setCurrentIndex(index);
    setScannedBin(null);
    setPutQty(String(entry.quantity));
    setProcessPhase('scan_bin');
    setPhase('process');

    // Get preferred bin suggestion
    try {
      const suggestResp = await client.get(`/api/putaway/suggest/${entry.item_id}`);
      setPreferredBin(suggestResp.data.preferred_bin || suggestResp.data.suggested_bin || null);
    } catch {
      setPreferredBin(null);
    }
  };

  const handleScanBin = async (barcode) => {
    try {
      const binResp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
      if (!binResp.data?.bin) {
        setError('Bin not found');
        setScanDisabled(true);
        return;
      }
      setScannedBin(binResp.data.bin);
      setProcessPhase('enter_qty');
    } catch {
      setError('Bin not found');
      setScanDisabled(true);
    }
  };

  const handleConfirmPutAway = async () => {
    const qty = parseInt(putQty, 10);
    if (!qty || qty <= 0) return;

    try {
      await client.post('/api/putaway/confirm', {
        item_id: activeItem.item_id,
        from_bin_id: activeItem.from_bin_id,
        to_bin_id: scannedBin.bin_id,
        quantity: qty,
        lot_number: activeItem.lot_number,
        warehouse_id: warehouseId,
      });

      setHistory((prev) => [...prev, {
        sku: activeItem.sku,
        item_name: activeItem.item_name,
        bin_code: scannedBin.bin_code,
        quantity: qty,
      }]);

      // Check preferred bin
      const matchesPreferred = preferredBin && scannedBin.bin_id === preferredBin.bin_id;
      if (matchesPreferred) {
        loadItem(currentIndex + 1);
      } else if (!preferredBin) {
        setPromptData({ type: 'set_new', item: activeItem, newBin: scannedBin, oldBin: null });
        setShowPreferredPrompt(true);
      } else {
        setPromptData({ type: 'change', item: activeItem, newBin: scannedBin, oldBin: preferredBin });
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
    loadItem(currentIndex + 1);
  };

  const handleSkipPreferred = () => {
    setShowPreferredPrompt(false);
    setPromptData(null);
    loadItem(currentIndex + 1);
  };

  const handleSkipItem = () => {
    loadItem(currentIndex + 1);
  };

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>PUT-AWAY</Text>
        {phase === 'load' && queue.length > 0 ? (
          <View style={styles.badge}>
            <Text style={styles.badgeText}>{queue.length}</Text>
          </View>
        ) : phase === 'process' ? (
          <Text style={{ fontFamily: fonts.mono, fontSize: 12, color: colors.textMuted }}>
            {currentIndex + 1} / {queue.length}
          </Text>
        ) : history.length > 0 ? (
          <View style={styles.badge}>
            <Text style={styles.badgeText}>{history.length}</Text>
          </View>
        ) : (
          <View style={{ width: 32 }} />
        )}
      </View>

      {/* Load Phase */}
      {phase === 'load' && (
        <>
          <View style={styles.content}>
            <View style={{ padding: 16, paddingBottom: 0 }}>
              <ScanInput placeholder="SCAN ITEM OR STAGING BIN" onScan={handleScanItem} disabled={scanDisabled} />
            </View>

            <View style={{ flex: 1, paddingHorizontal: 16 }}>
              <PagedList
                items={queue}
                pageSize={20}
                renderItem={(entry, index) => (
                  <View style={styles.queueRow}>
                    <View style={{ flex: 1 }}>
                      <Text style={styles.queueSku}>{entry.sku}</Text>
                      <Text style={styles.queueDetail}>
                        {entry.item_name} {'\u00b7'} QTY: {entry.quantity} {'\u00b7'} from {entry.from_bin_code}
                      </Text>
                    </View>
                    <TouchableOpacity style={styles.removeBtn} onPress={() => removeFromQueue(index)}>
                      <Text style={styles.removeText}>X</Text>
                    </TouchableOpacity>
                  </View>
                )}
              />
            </View>

            <View style={styles.bottomBar}>
              <TouchableOpacity
                style={[styles.buttonPrimary, queue.length === 0 && styles.buttonDisabled]}
                onPress={handleLoadAll}
                disabled={queue.length === 0}
              >
                <Text style={styles.buttonPrimaryText}>LOAD ALL ITEMS</Text>
              </TouchableOpacity>
            </View>
          </View>
        </>
      )}

      {/* Process Phase */}
      {phase === 'process' && activeItem && (
        <>
          <ScrollView style={styles.content} contentContainerStyle={styles.contentInner} keyboardShouldPersistTaps="handled">
            {/* Item info */}
            <View style={styles.itemCard}>
              <Text style={styles.itemName}>{activeItem.item_name}</Text>
              <Text style={styles.sku}>{activeItem.sku}</Text>
              <Text style={styles.fromBin}>FROM: {activeItem.from_bin_code} {'\u00b7'} QTY: {activeItem.quantity}</Text>
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

            {processPhase === 'scan_bin' && (
              <ScanInput placeholder="SCAN DESTINATION BIN" onScan={handleScanBin} disabled={scanDisabled} />
            )}

            {processPhase === 'enter_qty' && scannedBin && (
              <View style={styles.confirmCard}>
                <Text style={styles.confirmLabel}>DESTINATION</Text>
                <Text style={styles.confirmBinCode}>{scannedBin.bin_code}</Text>
                <View style={styles.qtyRow}>
                  <Text style={styles.qtyLabel}>QUANTITY</Text>
                  <TextInput
                    style={styles.qtyInput}
                    value={putQty}
                    onChangeText={setPutQty}
                    keyboardType="number-pad"
                  />
                </View>
                <TouchableOpacity style={styles.buttonPrimary} onPress={handleConfirmPutAway}>
                  <Text style={styles.buttonPrimaryText}>CONFIRM PUT-AWAY</Text>
                </TouchableOpacity>
                <TouchableOpacity
                  style={[styles.buttonSecondary, { marginTop: 8 }]}
                  onPress={() => { setScannedBin(null); setProcessPhase('scan_bin'); }}
                >
                  <Text style={styles.buttonSecondaryText}>SCAN DIFFERENT BIN</Text>
                </TouchableOpacity>
              </View>
            )}
          </ScrollView>

          <View style={styles.bottomBar}>
            <TouchableOpacity style={styles.buttonSecondary} onPress={handleSkipItem}>
              <Text style={styles.buttonSecondaryText}>SKIP ITEM</Text>
            </TouchableOpacity>
          </View>
        </>
      )}

      {/* Done Phase */}
      {phase === 'done' && (
        <View style={styles.doneSection}>
          <Text style={styles.doneCheck}>{'\u2713'}</Text>
          <Text style={styles.doneText}>Put-Away Complete</Text>
          <Text style={styles.doneDetail}>
            {history.length} item{history.length !== 1 ? 's' : ''} put away
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

          <TouchableOpacity style={[styles.buttonPrimary, { marginTop: 24, width: '100%' }]} onPress={() => { setPhase('load'); setQueue([]); }}>
            <Text style={styles.buttonPrimaryText}>PUT AWAY MORE</Text>
          </TouchableOpacity>
          <TouchableOpacity style={[styles.buttonSecondary, { marginTop: 8, width: '100%' }]} onPress={() => navigation.goBack()}>
            <Text style={styles.buttonSecondaryText}>DONE</Text>
          </TouchableOpacity>
        </View>
      )}

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

  // Load phase
  queueRow: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    padding: 14, marginBottom: 8, minHeight: 48,
  },
  queueSku: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  queueDetail: { fontSize: 12, color: colors.textMuted, marginTop: 2 },
  removeBtn: { padding: 8, minWidth: 48, minHeight: 48, alignItems: 'center', justifyContent: 'center' },
  removeText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textMuted },

  // Process phase
  itemCard: { marginBottom: 16 },
  itemName: { fontSize: 16, fontWeight: '600', color: colors.textPrimary },
  sku: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textMuted, marginTop: 2 },
  fromBin: { fontFamily: fonts.mono, fontSize: 12, color: colors.textMuted, marginTop: 4 },

  suggestCard: {
    borderWidth: 1.5, borderColor: colors.accentRed, borderRadius: 8,
    padding: 20, marginBottom: 16, alignItems: 'center',
  },
  suggestLabel: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3, marginBottom: 4 },
  suggestBinCode: { fontFamily: fonts.mono, fontSize: 30, fontWeight: '700', color: colors.accentRed },
  suggestZone: { fontFamily: fonts.mono, fontSize: 12, color: colors.copper, letterSpacing: 0.3, marginTop: 4, textTransform: 'uppercase' },

  noPreferredCard: {
    borderWidth: 1, borderColor: colors.border, borderRadius: 8, borderStyle: 'dashed',
    padding: 20, marginBottom: 16, alignItems: 'center',
  },
  noPreferredText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textMuted },
  noPreferredSub: { fontSize: 13, color: colors.textMuted, marginTop: 4 },

  confirmCard: {
    borderWidth: 1.5, borderColor: colors.success, borderRadius: 8,
    padding: 16, marginTop: 8,
  },
  confirmLabel: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3, marginBottom: 4 },
  confirmBinCode: { fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', color: colors.success, marginBottom: 12 },
  qtyRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 },
  qtyLabel: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3 },
  qtyInput: {
    fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary,
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    paddingHorizontal: 12, paddingVertical: 8, width: 80, textAlign: 'center', minHeight: 48,
  },

  // Done phase
  doneSection: { flex: 1, alignItems: 'center', padding: 32, paddingTop: 40 },
  doneCheck: { fontSize: 64, color: colors.success, marginBottom: 16 },
  doneText: { fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', color: colors.textPrimary, marginBottom: 8 },
  doneDetail: { fontSize: 15, color: colors.textMuted, marginBottom: 16 },

  // History
  historyRow: {
    flexDirection: 'row', alignItems: 'center', width: '100%',
    borderWidth: 1, borderColor: colors.success, borderRadius: 8,
    padding: 12, marginBottom: 6, minHeight: 48,
  },
  historyCheck: { fontSize: 16, color: colors.success, marginRight: 10 },
  historySku: { fontFamily: fonts.mono, fontSize: 13, fontWeight: '600', color: colors.textPrimary },
  historyDetail: { fontSize: 12, color: colors.textMuted, marginTop: 1 },
  historyBin: { fontFamily: fonts.mono, fontSize: 13, fontWeight: '700', color: colors.textPrimary },

  // Bottom bar
  bottomBar: { padding: 16, borderTopWidth: 1, borderTopColor: colors.border, gap: 8 },
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  buttonDisabled: { opacity: 0.5 },
  buttonSecondary: {
    backgroundColor: colors.background, borderWidth: 1.5, borderColor: colors.border, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonSecondaryText: { color: colors.textMuted, fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', letterSpacing: 0.5 },

  // Modal
  modalOverlay: { flex: 1, backgroundColor: 'rgba(0,0,0,0.4)', justifyContent: 'center', alignItems: 'center', padding: 32 },
  modalCard: { backgroundColor: colors.background, borderRadius: 8, padding: 24, width: '100%', maxWidth: 320 },
  modalTitle: { fontFamily: fonts.mono, fontSize: 12, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3 },
  modalItemName: { fontSize: 16, fontWeight: '600', color: colors.textPrimary, marginTop: 4 },
  modalSku: { fontFamily: fonts.mono, fontSize: 14, color: colors.textMuted, marginTop: 2 },
  modalDivider: { height: 1, backgroundColor: colors.border, marginVertical: 16 },
  modalBody: { fontSize: 14, color: colors.textPrimary, marginBottom: 20 },
  modalActions: { gap: 8 },
});
