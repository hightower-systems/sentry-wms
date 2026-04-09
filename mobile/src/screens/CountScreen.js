import React, { useState, useEffect, useCallback } from 'react';
import { View, Text, TouchableOpacity, TextInput, ScrollView, Vibration, StyleSheet } from 'react-native';
import ModeSelector from '../components/ModeSelector';
import AsyncStorage from '@react-native-async-storage/async-storage';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import useScanQueue from '../hooks/useScanQueue';
import useScreenError from '../hooks/useScreenError';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import ScreenHeader from '../components/ScreenHeader';
import { colors, fonts, radii, screenStyles, buttonStyles, listStyles, doneStyles } from '../theme/styles';

const MODE_KEY = 'sentry_count_mode';

export default function CountScreen({ navigation }) {
  const { warehouseId } = useAuth();
  const [countId, setCountId] = useState(null);
  const [binCode, setBinCode] = useState('');
  const [lines, setLines] = useState([]);
  const { error, scanDisabled, showError, clearError } = useScreenError();
  const [submitted, setSubmitted] = useState(false);
  const [mode, setMode] = useState('standard');
  const [showModeMenu, setShowModeMenu] = useState(false);
  const [turboStatus, setTurboStatus] = useState('');

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

  const handleScanBin = async (barcode) => {
    try {
      const binResp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
      if (!binResp.data?.bin) {
        showError('Bin not found');
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
      showError(err.response?.data?.error || 'Failed to create count');
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
      showError('Item not in this bin');
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
      showError(err.response?.data?.error || 'Failed to submit count');
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
    <View style={screenStyles.screen}>
      <ScreenHeader
        title="CYCLE COUNT"
        onBack={() => navigation.goBack()}
        right={
          countId && !submitted ? (
            <TouchableOpacity style={screenStyles.menuBtn} onPress={() => setShowModeMenu(true)}>
              <Text style={screenStyles.menuIcon}>{'\u22ee'}</Text>
            </TouchableOpacity>
          ) : undefined
        }
      />

      <ScrollView style={screenStyles.content} contentContainerStyle={screenStyles.contentInner} keyboardShouldPersistTaps="handled">
        {!countId ? (
          <ScanInput placeholder="SCAN BIN" onScan={handleScanBin} disabled={scanDisabled} />
        ) : submitted ? (
          <View style={styles.doneSection}>
            <Text style={doneStyles.check}>{'\u2713'}</Text>
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
                  style={[listStyles.row, hasVariance && styles.lineVariance]}
                >
                  <View style={{ flex: 1 }}>
                    <Text style={listStyles.sku}>{line.sku}</Text>
                    <Text style={listStyles.itemName}>{line.item_name}</Text>
                  </View>
                  {mode === 'standard' ? (
                    <TextInput
                      style={[styles.countInput, hasVariance && styles.countInputVariance]}
                      value={line.counted_quantity}
                      onChangeText={(val) => updateCount(index, val)}
                      keyboardType="number-pad"
                      placeholderTextColor={colors.textPlaceholder}
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
        <View style={screenStyles.bottomBar}>
          <TouchableOpacity style={buttonStyles.buttonPrimary} onPress={handleSubmit}>
            <Text style={buttonStyles.buttonPrimaryText}>SUBMIT COUNT</Text>
          </TouchableOpacity>
          <TouchableOpacity style={buttonStyles.buttonSecondary} onPress={() => navigation.goBack()}>
            <Text style={buttonStyles.buttonSecondaryText}>CANCEL</Text>
          </TouchableOpacity>
        </View>
      )}

      {submitted && (
        <View style={screenStyles.bottomBar}>
          <TouchableOpacity style={buttonStyles.buttonPrimary} onPress={resetCount}>
            <Text style={buttonStyles.buttonPrimaryText}>COUNT ANOTHER BIN</Text>
          </TouchableOpacity>
          <TouchableOpacity style={buttonStyles.buttonSecondary} onPress={() => navigation.goBack()}>
            <Text style={buttonStyles.buttonSecondaryText}>DONE</Text>
          </TouchableOpacity>
        </View>
      )}

      {/* Mode selector */}
      <ModeSelector
        visible={showModeMenu}
        onClose={() => setShowModeMenu(false)}
        title="COUNT MODE"
        mode={mode}
        onChangeMode={changeMode}
        standardDesc="Enter quantity for each item"
        turboDesc="Each scan = +1 to item count"
      />

      <ErrorPopup
        visible={!!error}
        message={error}
        onDismiss={clearError}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  binHeaderRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 },
  binHeader: { fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', color: colors.textPrimary },
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

  lineVariance: { borderColor: colors.copper },
  countInput: {
    fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary,
    borderWidth: 1, borderColor: colors.inputBorder, borderRadius: radii.input,
    backgroundColor: colors.inputBg,
    paddingHorizontal: 8, paddingVertical: 6, width: 70, textAlign: 'center', minHeight: 48,
  },
  countInputVariance: { borderColor: colors.copper, color: colors.copper },
  turboCount: {
    fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', color: colors.textPrimary,
    minWidth: 48, textAlign: 'center',
  },
  turboCountVariance: { color: colors.copper },

  doneSection: { alignItems: 'center', paddingVertical: 32 },
  doneText: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '600', color: colors.success, marginBottom: 8 },
  varianceNote: { fontSize: 13, color: colors.textMuted },

});
