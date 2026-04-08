import React, { useState, useEffect, useCallback } from 'react';
import { View, Text, TouchableOpacity, TextInput, ScrollView, Modal, Pressable, Vibration, StyleSheet } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import useScanQueue from '../hooks/useScanQueue';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

const MODE_KEY = 'sentry_count_mode';

export default function CountScreen({ navigation }) {
  const { warehouseId } = useAuth();
  const [countId, setCountId] = useState(null);
  const [binCode, setBinCode] = useState('');
  const [lines, setLines] = useState([]);
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [mode, setMode] = useState('standard');
  const [showModeMenu, setShowModeMenu] = useState(false);
  const [showExpected, setShowExpected] = useState(false);
  const [turboStatus, setTurboStatus] = useState('');

  useEffect(() => {
    AsyncStorage.getItem(MODE_KEY).then((saved) => {
      if (saved === 'turbo' || saved === 'standard') setMode(saved);
    }).catch(() => {});

    // Load show_expected setting (default is hidden for blind counts)
    client.get('/api/admin/settings/count_show_expected')
      .then((resp) => {
        const val = resp.data?.value;
        if (val === 'true' || val === true) setShowExpected(true);
      })
      .catch(() => {});
  }, []);

  const changeMode = (newMode) => {
    setMode(newMode);
    setShowModeMenu(false);
    AsyncStorage.setItem(MODE_KEY, newMode).catch(() => {});
  };

  const handleScanBin = async (barcode) => {
    try {
      const binResp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
      if (!binResp.data?.bin) {
        setError('Bin not found');
        setScanDisabled(true);
        return;
      }
      const binId = binResp.data.bin.bin_id;
      setBinCode(binResp.data.bin.bin_code);

      const createResp = await client.post('/api/inventory/cycle-count/create', {
        bin_ids: [binId],
        warehouse_id: warehouseId,
      });
      const newCountId = createResp.data.count_id || createResp.data.counts?.[0]?.count_id || createResp.data.count_ids?.[0];

      const detailResp = await client.get(`/api/inventory/cycle-count/${newCountId}`);
      setCountId(newCountId);
      setLines(
        (detailResp.data.lines || []).map((l) => ({
          ...l,
          counted_quantity: '0',
        }))
      );
      setSubmitted(false);
      setTurboStatus('');
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create count');
      setScanDisabled(true);
    }
  };

  const updateCount = (index, value) => {
    setLines((prev) => prev.map((l, i) => (i === index ? { ...l, counted_quantity: value } : l)));
  };

  // Turbo mode: each scan = +1 to that item's count
  const processTurboScan = useCallback(async (barcode) => {
    const index = lines.findIndex(
      (l) => l.upc === barcode || l.sku === barcode
    );
    if (index === -1) {
      setError('Item not in this bin');
      setScanDisabled(true);
      return;
    }

    setLines((prev) => {
      const updated = [...prev];
      const current = parseInt(updated[index].counted_quantity, 10) || 0;
      updated[index] = { ...updated[index], counted_quantity: String(current + 1) };

      const newCount = current + 1;
      setTurboStatus(`${updated[index].sku}: ${newCount} counted`);

      if (newCount >= updated[index].expected_quantity) {
        try { Vibration.vibrate(200); } catch {}
      }

      return updated;
    });
  }, [lines]);

  const [enqueueTurbo] = useScanQueue(processTurboScan);

  const handleScanItem = mode === 'turbo' ? enqueueTurbo : undefined;

  const handleSubmit = async () => {
    try {
      const countLines = lines.map((l) => ({
        count_line_id: l.count_line_id,
        counted_quantity: parseInt(l.counted_quantity, 10) || 0,
      }));
      await client.post('/api/inventory/cycle-count/submit', {
        count_id: countId,
        lines: countLines,
      });
      setSubmitted(true);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to submit count');
      setScanDisabled(true);
    }
  };

  const resetCount = () => {
    setCountId(null);
    setBinCode('');
    setLines([]);
    setSubmitted(false);
    setTurboStatus('');
  };

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>CYCLE COUNT</Text>
        {countId && !submitted ? (
          <TouchableOpacity style={styles.menuBtn} onPress={() => setShowModeMenu(true)}>
            <Text style={styles.menuIcon}>{'\u22ee'}</Text>
          </TouchableOpacity>
        ) : (
          <View style={{ width: 32 }} />
        )}
      </View>

      <ScrollView style={styles.content} contentContainerStyle={styles.contentInner} keyboardShouldPersistTaps="handled">
        {!countId ? (
          <ScanInput placeholder="SCAN BIN" onScan={handleScanBin} disabled={scanDisabled} />
        ) : submitted ? (
          <View style={styles.doneSection}>
            <Text style={styles.doneCheck}>{'\u2713'}</Text>
            <Text style={styles.doneText}>Count submitted for {binCode}</Text>
            {lines.some((l) => parseInt(l.counted_quantity, 10) !== l.expected_quantity) && (
              <Text style={styles.varianceNote}>Variances recorded and adjustments created</Text>
            )}
          </View>
        ) : (
          <>
            <View style={styles.binHeaderRow}>
              <Text style={styles.binHeader}>{binCode}</Text>
              <View style={[styles.modeBadge, mode === 'turbo' && styles.modeBadgeTurbo]}>
                <Text style={styles.modeBadgeText}>{mode === 'turbo' ? 'TURBO' : 'STANDARD'}</Text>
              </View>
            </View>

            {mode === 'turbo' && (
              <>
                <ScanInput placeholder="SCAN ITEM" onScan={handleScanItem} disabled={scanDisabled} />
                {turboStatus !== '' && (
                  <View style={styles.turboCard}>
                    <Text style={styles.turboText}>{turboStatus}</Text>
                  </View>
                )}
              </>
            )}

            {lines.map((line, index) => {
              const expected = line.expected_quantity;
              const counted = parseInt(line.counted_quantity, 10);
              const hasVariance = !isNaN(counted) && counted !== expected;
              return (
                <View
                  key={line.count_line_id || index}
                  style={[styles.lineRow, hasVariance && styles.lineVariance]}
                >
                  <View style={{ flex: 1 }}>
                    <Text style={styles.sku}>{line.sku}</Text>
                    <Text style={styles.itemName}>{line.item_name}</Text>
                    {showExpected && (
                      <Text style={styles.expected}>Expected: {expected}</Text>
                    )}
                  </View>
                  {mode === 'standard' ? (
                    <TextInput
                      style={[styles.countInput, hasVariance && styles.countInputVariance]}
                      value={line.counted_quantity}
                      onChangeText={(val) => updateCount(index, val)}
                      keyboardType="number-pad"
                    />
                  ) : (
                    <Text style={[styles.turboCount, hasVariance && styles.turboCountVariance]}>
                      {line.counted_quantity}
                    </Text>
                  )}
                </View>
              );
            })}
          </>
        )}
      </ScrollView>

      {/* Bottom bar */}
      {countId && !submitted && (
        <View style={styles.bottomBar}>
          <TouchableOpacity style={styles.buttonPrimary} onPress={handleSubmit}>
            <Text style={styles.buttonPrimaryText}>SUBMIT COUNT</Text>
          </TouchableOpacity>
          <TouchableOpacity style={styles.buttonCancel} onPress={() => navigation.goBack()}>
            <Text style={styles.buttonCancelText}>CANCEL</Text>
          </TouchableOpacity>
        </View>
      )}

      {submitted && (
        <View style={styles.bottomBar}>
          <TouchableOpacity style={styles.buttonPrimary} onPress={resetCount}>
            <Text style={styles.buttonPrimaryText}>COUNT ANOTHER BIN</Text>
          </TouchableOpacity>
          <TouchableOpacity style={styles.buttonCancel} onPress={() => navigation.goBack()}>
            <Text style={styles.buttonCancelText}>DONE</Text>
          </TouchableOpacity>
        </View>
      )}

      {/* Mode selector */}
      <Modal visible={showModeMenu} transparent animationType="fade">
        <Pressable style={styles.modeOverlay} onPress={() => setShowModeMenu(false)}>
          <View style={styles.modeCard}>
            <Text style={styles.modeTitle}>COUNT MODE</Text>
            <TouchableOpacity
              style={[styles.modeOption, mode === 'standard' && styles.modeOptionActive]}
              onPress={() => changeMode('standard')}
            >
              <Text style={[styles.modeOptionLabel, mode === 'standard' && styles.modeOptionLabelActive]}>STANDARD</Text>
              <Text style={styles.modeOptionDesc}>Enter quantity for each item</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[styles.modeOption, mode === 'turbo' && styles.modeOptionActive]}
              onPress={() => changeMode('turbo')}
            >
              <Text style={[styles.modeOptionLabel, mode === 'turbo' && styles.modeOptionLabelActive]}>TURBO</Text>
              <Text style={styles.modeOptionDesc}>Each scan = +1 to item count</Text>
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
  menuBtn: { padding: 4, minWidth: 32, minHeight: 48, justifyContent: 'center', alignItems: 'center' },
  menuIcon: { fontSize: 20, color: colors.textPrimary, fontWeight: '700' },
  content: { flex: 1 },
  contentInner: { padding: 16 },

  binHeaderRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 },
  binHeader: { fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', color: colors.textPrimary },
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

  lineRow: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    padding: 12, marginBottom: 8, minHeight: 48,
  },
  lineVariance: { borderColor: colors.copper },
  sku: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textPrimary },
  itemName: { fontSize: 12, color: colors.textMuted, marginTop: 1 },
  expected: { fontFamily: fonts.mono, fontSize: 11, color: colors.textMuted, marginTop: 2 },
  countInput: {
    fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary,
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    paddingHorizontal: 8, paddingVertical: 6, width: 70, textAlign: 'center', minHeight: 48,
  },
  countInputVariance: { borderColor: colors.copper, color: colors.copper },
  turboCount: {
    fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', color: colors.textPrimary,
    minWidth: 48, textAlign: 'center',
  },
  turboCountVariance: { color: colors.copper },

  bottomBar: { padding: 16, borderTopWidth: 1, borderTopColor: colors.border, gap: 8 },
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  buttonCancel: {
    backgroundColor: colors.background, borderWidth: 1.5, borderColor: colors.border, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonCancelText: { color: colors.textMuted, fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', letterSpacing: 0.5 },

  doneSection: { alignItems: 'center', paddingVertical: 32 },
  doneCheck: { fontSize: 64, color: colors.success, marginBottom: 16 },
  doneText: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '600', color: colors.success, marginBottom: 8 },
  varianceNote: { fontSize: 13, color: colors.textMuted },

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
