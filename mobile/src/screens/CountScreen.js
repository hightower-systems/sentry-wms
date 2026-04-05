import React, { useState } from 'react';
import { View, Text, TouchableOpacity, TextInput, ScrollView, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

export default function CountScreen({ navigation }) {
  const { warehouseId } = useAuth();
  const [countId, setCountId] = useState(null);
  const [binCode, setBinCode] = useState('');
  const [lines, setLines] = useState([]);
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const handleScanBin = async (barcode) => {
    try {
      // Look up the bin first to get bin_id
      const binResp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
      if (!binResp.data?.bin) {
        setError('Bin not found');
        setScanDisabled(true);
        return;
      }
      const binId = binResp.data.bin.bin_id;
      setBinCode(binResp.data.bin.bin_code);

      // Create cycle count
      const createResp = await client.post('/api/inventory/cycle-count/create', {
        bin_ids: [binId],
        warehouse_id: warehouseId,
      });
      const newCountId = createResp.data.count_id || createResp.data.count_ids?.[0];

      // Get count details
      const detailResp = await client.get(`/api/inventory/cycle-count/${newCountId}`);
      setCountId(newCountId);
      setLines(
        (detailResp.data.lines || []).map((l) => ({
          ...l,
          counted_quantity: String(l.expected_quantity),
        }))
      );
      setSubmitted(false);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to create count');
      setScanDisabled(true);
    }
  };

  const updateCount = (index, value) => {
    setLines((prev) => prev.map((l, i) => (i === index ? { ...l, counted_quantity: value } : l)));
  };

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
  };

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>CYCLE COUNT</Text>
        <View style={{ width: 32 }} />
      </View>

      <ScrollView style={styles.content} contentContainerStyle={styles.contentInner}>
        {!countId ? (
          <ScanInput placeholder="SCAN BIN" onScan={handleScanBin} disabled={scanDisabled} />
        ) : submitted ? (
          <View style={styles.doneSection}>
            <Text style={styles.doneText}>Count submitted for {binCode}</Text>
            {lines.some((l) => parseInt(l.counted_quantity, 10) !== l.expected_quantity) && (
              <Text style={styles.varianceNote}>Variances recorded and adjustments created</Text>
            )}
            <TouchableOpacity style={styles.buttonPrimary} onPress={resetCount}>
              <Text style={styles.buttonPrimaryText}>COUNT ANOTHER BIN</Text>
            </TouchableOpacity>
          </View>
        ) : (
          <>
            <Text style={styles.binHeader}>{binCode}</Text>

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
                    <Text style={styles.expected}>Expected: {expected}</Text>
                  </View>
                  <TextInput
                    style={[styles.countInput, hasVariance && styles.countInputVariance]}
                    value={line.counted_quantity}
                    onChangeText={(val) => updateCount(index, val)}
                    keyboardType="number-pad"
                  />
                </View>
              );
            })}

            <TouchableOpacity style={styles.buttonPrimary} onPress={handleSubmit}>
              <Text style={styles.buttonPrimaryText}>SUBMIT COUNT</Text>
            </TouchableOpacity>
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
  binHeader: { fontFamily: fonts.mono, fontSize: 22, fontWeight: '700', color: colors.textPrimary, marginBottom: 16 },
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
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48, marginTop: 16,
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  doneSection: { alignItems: 'center', paddingVertical: 32 },
  doneText: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '600', color: colors.success, marginBottom: 8 },
  varianceNote: { fontSize: 13, color: colors.textMuted, marginBottom: 24 },
});
