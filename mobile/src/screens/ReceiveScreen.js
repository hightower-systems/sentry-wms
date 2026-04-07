import React, { useState, useEffect, useCallback } from 'react';
import { View, Text, TouchableOpacity, ScrollView, TextInput, Modal, Pressable, Vibration, StyleSheet } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import PagedList from '../components/PagedList';
import useScanQueue from '../hooks/useScanQueue';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

const MODE_KEY = 'sentry_receive_mode';

export default function ReceiveScreen({ navigation }) {
  const { warehouseId } = useAuth();

  // Phase: 'scan_pos' → 'receiving' → 'done'
  const [phase, setPhase] = useState('scan_pos');

  // Phase 1: PO queue
  const [poQueue, setPoQueue] = useState([]);
  const [scanDisabled, setScanDisabled] = useState(false);

  // Phase 2: Receiving
  const [currentPoIndex, setCurrentPoIndex] = useState(0);
  const [po, setPo] = useState(null);
  const [lines, setLines] = useState([]);
  const [activeItem, setActiveItem] = useState(null);
  const [quantity, setQuantity] = useState('');
  const [mode, setMode] = useState('standard');
  const [showModeMenu, setShowModeMenu] = useState(false);
  const [turboStatus, setTurboStatus] = useState('');

  const [error, setError] = useState('');

  useEffect(() => {
    AsyncStorage.getItem(MODE_KEY).then((saved) => {
      if (saved === 'turbo' || saved === 'standard') setMode(saved);
    }).catch(() => {});
  }, []);

  const changeMode = (newMode) => {
    setMode(newMode);
    setShowModeMenu(false);
    AsyncStorage.setItem(MODE_KEY, newMode).catch(() => {});
  };

  // --- Phase 1: Scan POs to build queue ---

  const handleScanPO = async (barcode) => {
    // Duplicate check
    if (poQueue.find((p) => p.po_barcode === barcode || p.po_number === barcode)) {
      setError('Already scanned');
      setScanDisabled(true);
      return;
    }

    try {
      const resp = await client.get(`/api/receiving/po/${encodeURIComponent(barcode)}`);
      const poData = resp.data.purchase_order || resp.data.po || resp.data;
      const poLines = resp.data.lines || [];
      setPoQueue((prev) => [...prev, {
        po_id: poData.po_id,
        po_number: poData.po_number,
        po_barcode: poData.po_barcode || barcode,
        vendor_name: poData.vendor_name,
        line_count: poLines.length,
        total_units: poLines.reduce((sum, l) => sum + (l.quantity_ordered || 0), 0),
      }]);
    } catch (err) {
      if (err.response?.status === 404) {
        setError('PO not found');
      } else {
        setError(err.response?.data?.error || 'Validation failed');
      }
      setScanDisabled(true);
    }
  };

  const removePO = (po_id) => {
    setPoQueue((prev) => prev.filter((p) => p.po_id !== po_id));
  };

  const handleLoadAll = async () => {
    if (poQueue.length === 0) return;
    await loadPO(0);
  };

  // --- Phase 2: Receiving ---

  const loadPO = async (index) => {
    const entry = poQueue[index];
    if (!entry) {
      setPhase('done');
      return;
    }

    try {
      const resp = await client.get(`/api/receiving/po/${encodeURIComponent(entry.po_barcode || entry.po_number)}`);
      const poData = resp.data.purchase_order || resp.data.po || resp.data;
      setPo(poData);
      setLines(resp.data.lines || []);
      setActiveItem(null);
      setTurboStatus('');
      setCurrentPoIndex(index);
      setPhase('receiving');
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to load PO');
      setScanDisabled(true);
    }
  };

  const refreshPO = async () => {
    try {
      const resp = await client.get(`/api/receiving/po/${encodeURIComponent(po.po_barcode || po.po_number)}`);
      const updatedLines = resp.data.lines || [];
      setLines(updatedLines);
      setPo(resp.data.purchase_order || resp.data.po || resp.data);
      return updatedLines;
    } catch {
      return lines;
    }
  };

  const poComplete = lines.length > 0 && lines.every((l) => l.quantity_received >= l.quantity_ordered);

  // Standard mode
  const handleScanItemStandard = (barcode) => {
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

  const handleConfirmStandard = async () => {
    if (!activeItem) return;
    const qty = parseInt(quantity, 10);
    if (!qty || qty <= 0) return;

    const remaining = activeItem.quantity_ordered - activeItem.quantity_received;

    try {
      await client.post('/api/receiving/receive', {
        po_id: po.po_id,
        items: [{ item_id: activeItem.item_id, quantity: qty, bin_id: activeItem.staging_bin_id || 1 }],
        warehouse_id: warehouseId,
      });

      await refreshPO();
      setActiveItem(null);
      setQuantity('');

      if (qty > remaining && remaining > 0) {
        setError(`Receiving ${qty - remaining} over expected quantity`);
        setScanDisabled(true);
      }
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to receive');
      setScanDisabled(true);
    }
  };

  // Turbo mode
  const processTurboScan = useCallback(async (barcode) => {
    const match = lines.find(
      (l) => l.upc === barcode || l.sku === barcode || l.item_barcode === barcode
    );
    if (!match) {
      setError('Item not on this PO');
      setScanDisabled(true);
      return;
    }

    try {
      await client.post('/api/receiving/receive', {
        po_id: po.po_id,
        items: [{ item_id: match.item_id, quantity: 1, bin_id: match.staging_bin_id || 1 }],
        warehouse_id: warehouseId,
      });

      const updatedLines = await refreshPO();
      const updatedMatch = updatedLines.find((l) => l.item_id === match.item_id);
      const recv = updatedMatch?.quantity_received || match.quantity_received + 1;
      const ordered = match.quantity_ordered;

      setTurboStatus(`${match.item_name}: ${recv} / ${ordered}`);

      if (recv >= ordered) {
        try { Vibration.vibrate(200); } catch {}
      }
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to receive');
      setScanDisabled(true);
    }
  }, [lines, po, warehouseId]);

  const enqueueTurbo = useScanQueue(processTurboScan);

  const handleScanItem = mode === 'turbo' ? enqueueTurbo : handleScanItemStandard;

  const handleNextPO = () => {
    loadPO(currentPoIndex + 1);
  };

  const handleSubmit = () => {
    setPhase('done');
  };

  const handleCancel = () => {
    navigation.goBack();
  };

  const resetAll = () => {
    setPhase('scan_pos');
    setPoQueue([]);
    setPo(null);
    setLines([]);
    setActiveItem(null);
    setCurrentPoIndex(0);
    setTurboStatus('');
  };

  // --- Render ---

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>RECEIVE</Text>
        {phase === 'scan_pos' && poQueue.length > 0 ? (
          <View style={styles.badge}>
            <Text style={styles.badgeText}>{poQueue.length}</Text>
          </View>
        ) : phase === 'receiving' ? (
          <TouchableOpacity style={styles.menuBtn} onPress={() => setShowModeMenu(true)}>
            <Text style={styles.menuIcon}>{'\u22ee'}</Text>
          </TouchableOpacity>
        ) : (
          <View style={{ width: 32 }} />
        )}
      </View>

      {/* Phase 1: Scan POs */}
      {phase === 'scan_pos' && (
        <>
          <View style={styles.content}>
            <View style={{ padding: 16, paddingBottom: 0 }}>
              <ScanInput placeholder="SCAN PO" onScan={handleScanPO} disabled={scanDisabled} />
            </View>

            <View style={{ flex: 1, paddingHorizontal: 16 }}>
              <PagedList
                items={poQueue}
                pageSize={20}
                renderItem={(entry) => (
                  <View style={styles.queueRow}>
                    <View style={{ flex: 1 }}>
                      <Text style={styles.poNumber}>{entry.po_number}</Text>
                      <Text style={styles.poDetail}>
                        {entry.vendor_name} {'\u00b7'} {entry.line_count} item{entry.line_count !== 1 ? 's' : ''} {'\u00b7'} {entry.total_units} unit{entry.total_units !== 1 ? 's' : ''}
                      </Text>
                    </View>
                    <TouchableOpacity
                      style={styles.removeBtn}
                      onPress={() => removePO(entry.po_id)}
                    >
                      <Text style={styles.removeText}>X</Text>
                    </TouchableOpacity>
                  </View>
                )}
              />
            </View>

            <View style={styles.bottomBar}>
              <TouchableOpacity
                style={[styles.buttonPrimary, poQueue.length === 0 && styles.buttonDisabled]}
                onPress={handleLoadAll}
                disabled={poQueue.length === 0}
              >
                <Text style={styles.buttonPrimaryText}>LOAD ALL POs</Text>
              </TouchableOpacity>
            </View>
          </View>
        </>
      )}

      {/* Phase 2: Receiving */}
      {phase === 'receiving' && (
        <>
          <ScrollView style={styles.content} contentContainerStyle={styles.contentInner} keyboardShouldPersistTaps="handled">
            <View style={styles.poHeader}>
              <View style={styles.poHeaderRow}>
                <Text style={styles.poHeaderNumber}>{po.po_number}</Text>
                <Text style={styles.poProgress}>{currentPoIndex + 1} / {poQueue.length}</Text>
              </View>
              <View style={styles.poMeta}>
                <Text style={styles.poVendor}>{po.vendor_name}</Text>
                <View style={[styles.modeBadge, mode === 'turbo' && styles.modeBadgeTurbo]}>
                  <Text style={styles.modeBadgeText}>{mode === 'turbo' ? 'TURBO' : 'STANDARD'}</Text>
                </View>
              </View>
            </View>

            {poComplete ? (
              <View style={styles.poCompleteCard}>
                <Text style={styles.poCompleteText}>PO Complete</Text>
                <Text style={styles.poCompleteDetail}>{po.po_number} - all items received</Text>
                {currentPoIndex < poQueue.length - 1 && (
                  <TouchableOpacity style={styles.buttonPrimary} onPress={handleNextPO}>
                    <Text style={styles.buttonPrimaryText}>NEXT PO</Text>
                  </TouchableOpacity>
                )}
              </View>
            ) : (
              <>
                <ScanInput
                  placeholder="SCAN ITEM"
                  onScan={handleScanItem}
                  disabled={scanDisabled || (mode === 'standard' && !!activeItem)}
                />

                {mode === 'turbo' && turboStatus !== '' && (
                  <View style={styles.turboCard}>
                    <Text style={styles.turboText}>{turboStatus}</Text>
                  </View>
                )}

                {mode === 'standard' && activeItem && (
                  <View style={styles.receiveCard}>
                    <Text style={styles.sku}>{activeItem.sku}</Text>
                    <Text style={styles.itemName}>{activeItem.item_name}</Text>
                    <Text style={styles.expectedText}>
                      Expected: {activeItem.quantity_ordered} | Received: {activeItem.quantity_received}
                    </Text>
                    <View style={styles.qtyRow}>
                      <Text style={styles.label}>QUANTITY</Text>
                      <TextInput
                        style={styles.qtyInput}
                        value={quantity}
                        onChangeText={setQuantity}
                        keyboardType="number-pad"
                      />
                    </View>
                    <TouchableOpacity style={styles.buttonPrimary} onPress={handleConfirmStandard}>
                      <Text style={styles.buttonPrimaryText}>RECEIVE</Text>
                    </TouchableOpacity>
                  </View>
                )}

                {lines.map((line) => {
                  const done = line.quantity_received >= line.quantity_ordered;
                  return (
                    <View key={line.po_line_id || line.item_id} style={[styles.lineRow, done && styles.lineRowDone]}>
                      <View style={{ flex: 1 }}>
                        <Text style={[styles.sku, done ? styles.textDone : styles.textPending]}>{line.sku}</Text>
                        <Text style={styles.itemName}>{line.item_name}</Text>
                      </View>
                      <Text style={[styles.lineQty, done ? styles.textDone : styles.textPending]}>
                        {line.quantity_received}/{line.quantity_ordered}
                      </Text>
                    </View>
                  );
                })}
              </>
            )}
          </ScrollView>

          <View style={styles.bottomBar}>
            <TouchableOpacity style={styles.buttonPrimary} onPress={handleSubmit}>
              <Text style={styles.buttonPrimaryText}>SUBMIT</Text>
            </TouchableOpacity>
            <TouchableOpacity style={styles.buttonCancel} onPress={handleCancel}>
              <Text style={styles.buttonCancelText}>CANCEL</Text>
            </TouchableOpacity>
          </View>
        </>
      )}

      {/* Phase 3: Done */}
      {phase === 'done' && (
        <View style={styles.doneSection}>
          <Text style={styles.doneCheck}>{'\u2713'}</Text>
          <Text style={styles.doneText}>Receiving Complete</Text>
          <Text style={styles.doneDetail}>
            {poQueue.length} PO{poQueue.length !== 1 ? 's' : ''} processed
          </Text>
          <TouchableOpacity style={styles.buttonPrimary} onPress={resetAll}>
            <Text style={styles.buttonPrimaryText}>RECEIVE MORE</Text>
          </TouchableOpacity>
          <TouchableOpacity style={[styles.buttonCancel, { marginTop: 8 }]} onPress={() => navigation.goBack()}>
            <Text style={styles.buttonCancelText}>DONE</Text>
          </TouchableOpacity>
        </View>
      )}

      {/* Mode selector modal */}
      <Modal visible={showModeMenu} transparent animationType="fade">
        <Pressable style={styles.modeOverlay} onPress={() => setShowModeMenu(false)}>
          <View style={styles.modeCard}>
            <Text style={styles.modeTitle}>RECEIVE MODE</Text>
            <TouchableOpacity
              style={[styles.modeOption, mode === 'standard' && styles.modeOptionActive]}
              onPress={() => changeMode('standard')}
            >
              <Text style={[styles.modeOptionLabel, mode === 'standard' && styles.modeOptionLabelActive]}>STANDARD</Text>
              <Text style={styles.modeOptionDesc}>Scan item, enter qty, confirm</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[styles.modeOption, mode === 'turbo' && styles.modeOptionActive]}
              onPress={() => changeMode('turbo')}
            >
              <Text style={[styles.modeOptionLabel, mode === 'turbo' && styles.modeOptionLabelActive]}>TURBO</Text>
              <Text style={styles.modeOptionDesc}>Each scan = 1 unit received</Text>
            </TouchableOpacity>
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
  badge: {
    backgroundColor: colors.accentRed, borderRadius: 10,
    paddingHorizontal: 8, paddingVertical: 2, minWidth: 24, alignItems: 'center',
  },
  badgeText: { color: '#FFFFFF', fontFamily: fonts.mono, fontSize: 12, fontWeight: '700' },
  menuBtn: { padding: 4, minWidth: 32, minHeight: 48, justifyContent: 'center', alignItems: 'center' },
  menuIcon: { fontSize: 20, color: colors.textPrimary, fontWeight: '700' },
  content: { flex: 1 },
  contentInner: { padding: 16 },

  // Phase 1: PO queue
  queueRow: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    padding: 14, marginBottom: 8, minHeight: 48,
  },
  poNumber: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  poDetail: { fontSize: 12, color: colors.textMuted, marginTop: 2 },
  removeBtn: { padding: 8, minWidth: 48, minHeight: 48, alignItems: 'center', justifyContent: 'center' },
  removeText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textMuted },
  bottomBar: { padding: 16, borderTopWidth: 1, borderTopColor: colors.border, gap: 8 },
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  buttonDisabled: { opacity: 0.5 },
  buttonCancel: {
    backgroundColor: colors.background, borderWidth: 1.5, borderColor: colors.border, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonCancelText: { color: colors.textMuted, fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', letterSpacing: 0.5 },

  // Phase 2: Receiving
  poHeader: { marginBottom: 16 },
  poHeaderRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  poHeaderNumber: { fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary },
  poProgress: { fontFamily: fonts.mono, fontSize: 12, color: colors.textMuted },
  poMeta: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginTop: 2 },
  poVendor: { fontSize: 13, color: colors.textMuted },
  modeBadge: {
    backgroundColor: colors.border, borderRadius: 4,
    paddingHorizontal: 8, paddingVertical: 2,
  },
  modeBadgeTurbo: { backgroundColor: colors.accentRed },
  modeBadgeText: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '700', color: colors.cream, letterSpacing: 0.5 },
  turboCard: {
    backgroundColor: '#f0f9f0', borderWidth: 1, borderColor: colors.success, borderRadius: 8,
    padding: 12, marginBottom: 16, alignItems: 'center',
  },
  turboText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.success },
  receiveCard: {
    borderWidth: 1.5, borderColor: colors.accentRed, borderRadius: 8,
    padding: 16, marginBottom: 16,
  },
  sku: { fontFamily: fonts.mono, fontSize: 14, color: colors.textPrimary, fontWeight: '600' },
  itemName: { fontSize: 13, color: colors.textMuted, marginTop: 2 },
  expectedText: { fontFamily: fonts.mono, fontSize: 12, color: colors.textMuted, marginTop: 6 },
  qtyRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginVertical: 12 },
  label: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3 },
  qtyInput: {
    fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary,
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    paddingHorizontal: 12, paddingVertical: 8, width: 80, textAlign: 'center', minHeight: 48,
  },
  lineRow: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    padding: 12, marginBottom: 8, minHeight: 48,
  },
  lineQty: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  lineRowDone: { borderColor: colors.success },
  textDone: { color: colors.success },
  textPending: { color: colors.accentRed },

  // PO complete within receiving phase
  poCompleteCard: { alignItems: 'center', paddingVertical: 24 },
  poCompleteText: { fontFamily: fonts.mono, fontSize: 20, fontWeight: '700', color: colors.success, marginBottom: 4 },
  poCompleteDetail: { fontFamily: fonts.mono, fontSize: 13, color: colors.textMuted, marginBottom: 24 },

  // Phase 3: Done
  doneSection: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 32 },
  doneCheck: { fontSize: 64, color: colors.success, marginBottom: 16 },
  doneText: { fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', color: colors.textPrimary, marginBottom: 8 },
  doneDetail: { fontSize: 15, color: colors.textMuted, marginBottom: 32 },

  // Mode selector
  modeOverlay: {
    flex: 1, backgroundColor: 'rgba(0,0,0,0.4)',
    justifyContent: 'flex-start', alignItems: 'flex-end',
    paddingTop: 100, paddingRight: 16,
  },
  modeCard: {
    backgroundColor: colors.background, borderRadius: 8, padding: 16, minWidth: 220,
    borderWidth: 1, borderColor: colors.border,
    elevation: 4, shadowColor: '#000', shadowOffset: { width: 0, height: 2 }, shadowOpacity: 0.15, shadowRadius: 4,
  },
  modeTitle: { fontFamily: fonts.mono, fontSize: 12, fontWeight: '700', color: colors.textMuted, letterSpacing: 0.5, marginBottom: 12 },
  modeOption: {
    padding: 12, borderRadius: 6, borderWidth: 1, borderColor: colors.border, marginBottom: 8,
  },
  modeOptionActive: { borderColor: colors.accentRed, backgroundColor: '#fdf6f4' },
  modeOptionLabel: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  modeOptionLabelActive: { color: colors.accentRed },
  modeOptionDesc: { fontSize: 12, color: colors.textMuted, marginTop: 2 },
});
