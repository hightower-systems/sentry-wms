import React, { useState, useEffect, useCallback } from 'react';
import { View, Text, TouchableOpacity, ScrollView, TextInput, Modal, Vibration, Alert, StyleSheet } from 'react-native';
import ModeSelector from '../components/ModeSelector';
import AsyncStorage from '@react-native-async-storage/async-storage';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import PagedList from '../components/PagedList';
import useScanQueue from '../hooks/useScanQueue';
import useScreenError from '../hooks/useScreenError';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import ScreenHeader from '../components/ScreenHeader';
import { colors, fonts, radii, screenStyles, buttonStyles, listStyles, doneStyles } from '../theme/styles';

const MODE_KEY = 'sentry_receive_mode';

export default function ReceiveScreen({ navigation }) {
  const { warehouseId } = useAuth();

  // Phase: 'scan_pos' → 'receiving' → 'done'
  const [phase, setPhase] = useState('scan_pos');

  // Phase 1: PO queue
  const [poQueue, setPoQueue] = useState([]);
  const { error, scanDisabled, showError, clearError } = useScreenError();

  // Phase 2: Receiving
  const [currentPoIndex, setCurrentPoIndex] = useState(0);
  const [po, setPo] = useState(null);
  const [lines, setLines] = useState([]);
  const [activeItem, setActiveItem] = useState(null);
  const [quantity, setQuantity] = useState('');
  const [mode, setMode] = useState('standard');
  const [showModeMenu, setShowModeMenu] = useState(false);
  const [turboStatus, setTurboStatus] = useState('');
  const [receivingBinId, setReceivingBinId] = useState(null);
  const [receivingBinCode, setReceivingBinCode] = useState('');
  const [showBinPicker, setShowBinPicker] = useState(false);
  const [binPickerValue, setBinPickerValue] = useState('');
  const [allowOverReceiving, setAllowOverReceiving] = useState(true);

  useEffect(() => {
    AsyncStorage.getItem(MODE_KEY).then((saved) => {
      if (saved === 'turbo' || saved === 'standard') setMode(saved);
    }).catch(() => {});
    // Load over-receiving setting
    client.get('/api/admin/settings/allow_over_receiving')
      .then((resp) => {
        const val = resp.data?.value;
        setAllowOverReceiving(val !== 'false' && val !== false);
      })
      .catch(() => {});
    // Load default receiving bin from settings
    client.get('/api/admin/settings/default_receiving_bin')
      .then((resp) => {
        const binId = parseInt(resp.data?.value, 10);
        if (binId) {
          setReceivingBinId(binId);
          // Look up bin code via admin bins list
          client.get(`/api/admin/bins?warehouse_id=${warehouseId}`)
            .then((r) => {
              const bins = r.data?.bins || [];
              const match = bins.find((b) => b.id === binId);
              setReceivingBinCode(match?.bin_code || `Bin #${binId}`);
            })
            .catch(() => setReceivingBinCode(`Bin #${binId}`));
        }
      })
      .catch(() => {});
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
      showError('Already scanned');
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
        showError('PO not found');
      } else {
        showError(err.response?.data?.error || 'Validation failed');
      }
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
      showError(err.response?.data?.error || 'Failed to load PO');
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
      showError('Item not on this PO');
      return;
    }
    const remaining = match.quantity_ordered - match.quantity_received;
    setActiveItem(match);
    setQuantity(String(remaining > 0 ? remaining : 1));
  };

  const doReceiveStandard = async (qty) => {
    try {
      await client.post('/api/receiving/receive', {
        po_id: po.po_id,
        items: [{ item_id: activeItem.item_id, quantity: qty, bin_id: receivingBinId || activeItem.staging_bin_id || 1 }],
        warehouse_id: warehouseId,
      });

      await refreshPO();
      setActiveItem(null);
      setQuantity('');
    } catch (err) {
      showError(err.response?.data?.error || 'Failed to receive');
    }
  };

  const handleConfirmStandard = async () => {
    if (!activeItem) return;
    const qty = parseInt(quantity, 10);
    if (!qty || qty <= 0) return;

    const remaining = activeItem.quantity_ordered - activeItem.quantity_received;

    if (qty > remaining && remaining > 0) {
      if (!allowOverReceiving) {
        showError(`Cannot receive more than ordered (${remaining} remaining)`);
        return;
      }
      // Show warning but allow
      Alert.alert(
        'Over-Receiving',
        `You are receiving ${qty - remaining} more than expected. Continue?`,
        [
          { text: 'Cancel', style: 'cancel' },
          { text: 'Continue', onPress: () => doReceiveStandard(qty) },
        ]
      );
      return;
    }

    await doReceiveStandard(qty);
  };

  // Turbo mode
  const processTurboScan = useCallback(async (barcode) => {
    const match = lines.find(
      (l) => l.upc === barcode || l.sku === barcode || l.item_barcode === barcode
    );
    if (!match) {
      showError('Item not on this PO');
      return;
    }

    try {
      await client.post('/api/receiving/receive', {
        po_id: po.po_id,
        items: [{ item_id: match.item_id, quantity: 1, bin_id: receivingBinId || match.staging_bin_id || 1 }],
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
      showError(err.response?.data?.error || 'Failed to receive');
    }
  }, [lines, po, warehouseId, receivingBinId, showError]);

  const [enqueueTurbo, turboProcessing] = useScanQueue(processTurboScan);

  const handleScanItem = mode === 'turbo' ? enqueueTurbo : handleScanItemStandard;

  const handleNextPO = () => {
    loadPO(currentPoIndex + 1);
  };

  const handleSubmit = () => {
    setPhase('done');
  };

  const handleCancel = () => {
    const hasReceived = lines.some((l) => l.quantity_received > 0);
    if (!hasReceived) {
      navigation.goBack();
      return;
    }
    Alert.alert(
      'Cancel Receiving',
      'Are you sure you want to cancel? Received items will not be saved.',
      [
        { text: 'Go Back', style: 'cancel' },
        { text: 'Yes, Cancel', style: 'destructive', onPress: () => navigation.goBack() },
      ]
    );
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
    <View style={screenStyles.screen}>
      <ScreenHeader
        title="RECEIVE"
        onBack={() => navigation.goBack()}
        right={
          phase === 'scan_pos' && poQueue.length > 0 ? (
            <View style={styles.badge}>
              <Text style={styles.badgeText}>{poQueue.length}</Text>
            </View>
          ) : phase === 'receiving' ? (
            <TouchableOpacity style={screenStyles.menuBtn} onPress={() => setShowModeMenu(true)}>
              <Text style={screenStyles.menuIcon}>{'\u22ee'}</Text>
            </TouchableOpacity>
          ) : undefined
        }
      />

      {/* Phase 1: Scan POs */}
      {phase === 'scan_pos' && (
        <>
          <View style={screenStyles.content}>
            <View style={{ padding: 16, paddingBottom: 0 }}>
              <ScanInput placeholder="SCAN PO" onScan={handleScanPO} disabled={scanDisabled} />
            </View>

            <View style={{ flex: 1, paddingHorizontal: 16 }}>
              <PagedList
                items={poQueue}
                pageSize={20}
                renderItem={(entry) => (
                  <View style={[listStyles.row, { padding: 14 }]}>
                    <View style={{ flex: 1 }}>
                      <Text style={styles.poNumber}>{entry.po_number}</Text>
                      <Text style={styles.poDetail}>
                        {entry.vendor_name} {'\u00b7'} {entry.line_count} item{entry.line_count !== 1 ? 's' : ''} {'\u00b7'} {entry.total_units} unit{entry.total_units !== 1 ? 's' : ''}
                      </Text>
                    </View>
                    <TouchableOpacity
                      style={listStyles.removeBtn}
                      onPress={() => removePO(entry.po_id)}
                    >
                      <Text style={listStyles.removeText}>X</Text>
                    </TouchableOpacity>
                  </View>
                )}
              />
            </View>

            <View style={screenStyles.bottomBar}>
              <TouchableOpacity
                style={[buttonStyles.buttonPrimary, poQueue.length === 0 && buttonStyles.buttonDisabled]}
                onPress={handleLoadAll}
                disabled={poQueue.length === 0}
              >
                <Text style={buttonStyles.buttonPrimaryText}>LOAD ALL POs</Text>
              </TouchableOpacity>
            </View>
          </View>
        </>
      )}

      {/* Phase 2: Receiving */}
      {phase === 'receiving' && (
        <>
          <ScrollView style={screenStyles.content} contentContainerStyle={screenStyles.contentInner} keyboardShouldPersistTaps="handled">
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
              {receivingBinCode ? (
                <Text style={{ fontFamily: fonts.mono, fontSize: 11, color: colors.textMuted, marginTop: 4 }}>
                  {'\u2192'} {receivingBinCode}
                </Text>
              ) : null}
            </View>

            {poComplete ? (
              <View style={styles.poCompleteCard}>
                <Text style={styles.poCompleteText}>PO Complete</Text>
                <Text style={styles.poCompleteDetail}>{po.po_number} - all items received</Text>
                {currentPoIndex < poQueue.length - 1 && (
                  <TouchableOpacity style={buttonStyles.buttonPrimary} onPress={handleNextPO}>
                    <Text style={buttonStyles.buttonPrimaryText}>NEXT PO</Text>
                  </TouchableOpacity>
                )}
              </View>
            ) : (
              <>
                <ScanInput
                  placeholder="SCAN ITEM"
                  onScan={handleScanItem}
                  disabled={scanDisabled || (mode === 'standard' && !!activeItem) || (mode === 'turbo' && turboProcessing)}
                />

                {mode === 'turbo' && turboStatus !== '' && (
                  <View style={styles.turboCard}>
                    <Text style={styles.turboText}>{turboStatus}</Text>
                  </View>
                )}

                {mode === 'standard' && activeItem && (
                  <View style={styles.receiveCard}>
                    <Text style={listStyles.sku}>{activeItem.sku}</Text>
                    <Text style={[listStyles.itemName, { fontSize: 13 }]}>{activeItem.item_name}</Text>
                    <Text style={styles.expectedText}>
                      Expected: {activeItem.quantity_ordered} | Received: {activeItem.quantity_received}
                    </Text>
                    <View style={styles.qtyRow}>
                      <Text style={listStyles.label}>QUANTITY</Text>
                      <TextInput
                        style={listStyles.qtyInput}
                        value={quantity}
                        onChangeText={setQuantity}
                        keyboardType="number-pad"
                        placeholderTextColor={colors.textPlaceholder}
                      />
                    </View>
                    <TouchableOpacity style={buttonStyles.buttonPrimary} onPress={handleConfirmStandard}>
                      <Text style={buttonStyles.buttonPrimaryText}>RECEIVE</Text>
                    </TouchableOpacity>
                  </View>
                )}

                {lines.map((line) => {
                  const done = line.quantity_received >= line.quantity_ordered;
                  return (
                    <View key={line.po_line_id || line.item_id} style={[listStyles.row, done && styles.lineRowDone]}>
                      <View style={{ flex: 1 }}>
                        <Text style={[listStyles.sku, done ? styles.textDone : styles.textPending]}>{line.sku}</Text>
                        <Text style={[listStyles.itemName, { fontSize: 13 }]}>{line.item_name}</Text>
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

          <View style={screenStyles.bottomBar}>
            <TouchableOpacity style={buttonStyles.buttonPrimary} onPress={handleSubmit}>
              <Text style={buttonStyles.buttonPrimaryText}>SUBMIT</Text>
            </TouchableOpacity>
            <TouchableOpacity style={buttonStyles.buttonSecondary} onPress={handleCancel}>
              <Text style={buttonStyles.buttonSecondaryText}>CANCEL</Text>
            </TouchableOpacity>
          </View>
        </>
      )}

      {/* Phase 3: Done */}
      {phase === 'done' && (
        <View style={doneStyles.section}>
          <Text style={doneStyles.check}>{'\u2713'}</Text>
          <Text style={doneStyles.title}>Receiving Complete</Text>
          <Text style={doneStyles.detail}>
            {poQueue.length} PO{poQueue.length !== 1 ? 's' : ''} processed
          </Text>
          <TouchableOpacity style={buttonStyles.buttonPrimary} onPress={resetAll}>
            <Text style={buttonStyles.buttonPrimaryText}>RECEIVE MORE</Text>
          </TouchableOpacity>
          <TouchableOpacity style={[buttonStyles.buttonSecondary, { marginTop: 8 }]} onPress={() => navigation.goBack()}>
            <Text style={buttonStyles.buttonSecondaryText}>DONE</Text>
          </TouchableOpacity>
        </View>
      )}

      {/* Mode selector modal */}
      <ModeSelector
        visible={showModeMenu}
        onClose={() => setShowModeMenu(false)}
        title="RECEIVE MODE"
        mode={mode}
        onChangeMode={changeMode}
        standardDesc="Scan item, enter qty, confirm"
        turboDesc="Each scan = 1 unit received"
      >
        <View style={{ height: 1, backgroundColor: colors.cardBorder, marginVertical: 8 }} />
        <Text style={styles.modeTitle}>RECEIVING BIN</Text>
        <TouchableOpacity
          style={styles.modeOption}
          onPress={() => { setShowModeMenu(false); setBinPickerValue(''); setShowBinPicker(true); }}
        >
          <Text style={styles.modeOptionLabel}>{receivingBinCode || 'Not Set'}</Text>
          <Text style={styles.modeOptionDesc}>Tap to change destination bin</Text>
        </TouchableOpacity>
      </ModeSelector>

      {/* Bin picker modal */}
      <Modal visible={showBinPicker} transparent animationType="fade">
        <View style={styles.modeOverlay}>
          <View style={styles.modeCard}>
            <Text style={styles.modeTitle}>CHANGE RECEIVING BIN</Text>
            <Text style={{ fontSize: 12, color: colors.textMuted, marginBottom: 12 }}>
              Scan or type bin code
            </Text>
            <ScanInput
              placeholder="SCAN BIN"
              onScan={async (barcode) => {
                try {
                  const resp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
                  if (resp.data?.bin) {
                    setReceivingBinId(resp.data.bin.bin_id);
                    setReceivingBinCode(resp.data.bin.bin_code);
                    setShowBinPicker(false);
                  } else {
                    showError('Bin not found');
                  }
                } catch {
                  showError('Bin not found');
                }
              }}
              disabled={false}
            />
            <TouchableOpacity
              style={[buttonStyles.buttonSecondary, { marginTop: 8 }]}
              onPress={() => setShowBinPicker(false)}
            >
              <Text style={buttonStyles.buttonSecondaryText}>CANCEL</Text>
            </TouchableOpacity>
          </View>
        </View>
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
  badge: {
    backgroundColor: colors.accentRed, borderRadius: 10,
    paddingHorizontal: 8, paddingVertical: 2, minWidth: 24, alignItems: 'center',
  },
  badgeText: { color: '#FFFFFF', fontFamily: fonts.mono, fontSize: 12, fontWeight: '700' },

  // Phase 1: PO queue
  poNumber: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  poDetail: { fontSize: 12, color: colors.textMuted, marginTop: 2 },

  // Phase 2: Receiving
  poHeader: { marginBottom: 16 },
  poHeaderRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  poHeaderNumber: { fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary },
  poProgress: { fontFamily: fonts.mono, fontSize: 12, color: colors.textMuted },
  poMeta: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginTop: 2 },
  poVendor: { fontSize: 13, color: colors.textMuted },
  modeBadge: {
    backgroundColor: colors.cardBorder, borderRadius: radii.badge,
    paddingHorizontal: 8, paddingVertical: 2,
  },
  modeBadgeTurbo: { backgroundColor: colors.accentRed },
  modeBadgeText: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '700', color: colors.cream, letterSpacing: 0.5 },
  turboCard: {
    backgroundColor: '#f0f9f0', borderWidth: 1, borderColor: colors.success, borderRadius: radii.card,
    padding: 12, marginBottom: 16, alignItems: 'center',
  },
  turboText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.success },
  receiveCard: {
    borderWidth: 1.5, borderColor: colors.accentRed, borderRadius: radii.card,
    padding: 16, marginBottom: 16,
  },
  expectedText: { fontFamily: fonts.mono, fontSize: 12, color: colors.textMuted, marginTop: 6 },
  qtyRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginVertical: 12 },
  lineQty: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  lineRowDone: { borderColor: colors.success },
  textDone: { color: colors.success },
  textPending: { color: colors.accentRed },

  // PO complete within receiving phase
  poCompleteCard: { alignItems: 'center', paddingVertical: 24 },
  poCompleteText: { fontFamily: fonts.mono, fontSize: 20, fontWeight: '700', color: colors.success, marginBottom: 4 },
  poCompleteDetail: { fontFamily: fonts.mono, fontSize: 13, color: colors.textMuted, marginBottom: 24 },

  // Mode selector
  modeOverlay: {
    flex: 1, backgroundColor: colors.overlay,
    justifyContent: 'flex-start', alignItems: 'flex-end',
    paddingTop: 100, paddingRight: 16,
  },
  modeCard: {
    backgroundColor: colors.background, borderRadius: radii.card, padding: 16, minWidth: 220,
    borderWidth: 1, borderColor: colors.cardBorder,
  },
  modeTitle: { fontFamily: fonts.mono, fontSize: 12, fontWeight: '700', color: colors.textMuted, letterSpacing: 0.5, marginBottom: 12 },
  modeOption: {
    padding: 12, borderRadius: radii.badge, borderWidth: 1, borderColor: colors.cardBorder, marginBottom: 8,
  },
  modeOptionLabel: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  modeOptionDesc: { fontSize: 12, color: colors.textMuted, marginTop: 2 },
});
