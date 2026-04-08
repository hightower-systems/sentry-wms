import React, { useState } from 'react';
import { View, Text, TouchableOpacity, TextInput, ScrollView, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

const STEPS = ['SCAN ITEM', 'SCAN FROM BIN', 'SCAN TO BIN'];

export default function TransferScreen({ navigation }) {
  const { warehouseId } = useAuth();
  const [step, setStep] = useState(0);
  const [item, setItem] = useState(null);
  const [locations, setLocations] = useState([]);
  const [fromBin, setFromBin] = useState(null);
  const [toBin, setToBin] = useState(null);
  const [quantity, setQuantity] = useState('1');
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);
  const [success, setSuccess] = useState(false);

  const handleScan = async (barcode) => {
    if (step === 0) {
      // Scan item
      try {
        const resp = await client.get(`/api/lookup/item/${encodeURIComponent(barcode)}`);
        if (!resp.data?.item) {
          setError('Item not found');
          setScanDisabled(true);
          return;
        }
        setItem(resp.data.item);
        setLocations(resp.data.locations || []);
        setStep(1);
      } catch {
        setError('Item not found');
        setScanDisabled(true);
      }
    } else if (step === 1) {
      // Scan from bin
      try {
        const resp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
        if (!resp.data?.bin) {
          setError('Bin not found');
          setScanDisabled(true);
          return;
        }
        const bin = resp.data.bin;
        // Check item is in this bin
        const inBin = locations.find(
          (l) => l.bin_id === bin.bin_id || l.bin_code === bin.bin_code
        );
        if (!inBin) {
          setError('Item not found in this bin');
          setScanDisabled(true);
          return;
        }
        setFromBin({ ...bin, available: inBin.quantity_on_hand });
        setQuantity('1');
        setStep(2);
      } catch {
        setError('Bin not found');
        setScanDisabled(true);
      }
    } else if (step === 2) {
      // Scan to bin
      try {
        const resp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
        if (!resp.data?.bin) {
          setError('Bin not found');
          setScanDisabled(true);
          return;
        }
        setToBin(resp.data.bin);
        setStep(3);
      } catch {
        setError('Bin not found');
        setScanDisabled(true);
      }
    }
  };

  const handleConfirm = async () => {
    const qty = parseInt(quantity, 10);
    if (!qty || qty <= 0) return;

    try {
      await client.post('/api/transfers/move', {
        item_id: item.item_id,
        from_bin_id: fromBin.bin_id,
        to_bin_id: toBin.bin_id,
        quantity: qty,
        warehouse_id: warehouseId,
      });
      setSuccess(true);
    } catch (err) {
      setError(err.response?.data?.error || 'Transfer failed');
      setScanDisabled(true);
    }
  };

  const resetAll = () => {
    setStep(0);
    setItem(null);
    setLocations([]);
    setFromBin(null);
    setToBin(null);
    setQuantity('1');
    setSuccess(false);
  };

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>TRANSFER</Text>
        <View style={{ width: 32 }} />
      </View>

      <ScrollView style={styles.content} contentContainerStyle={styles.contentInner} keyboardShouldPersistTaps="handled">
        {success ? (
          <View style={styles.successSection}>
            <Text style={styles.successText}>Transfer complete</Text>
            <Text style={styles.successDetail}>
              {quantity}x {item?.sku} moved from {fromBin?.bin_code} to {toBin?.bin_code}
            </Text>
            <TouchableOpacity style={[styles.buttonPrimary, { width: '100%' }]} onPress={resetAll}>
              <Text style={styles.buttonPrimaryText}>NEW TRANSFER</Text>
            </TouchableOpacity>
            <TouchableOpacity style={[styles.buttonSecondary, { width: '100%', marginTop: 8 }]} onPress={() => navigation.goBack()}>
              <Text style={styles.buttonSecondaryText}>DONE</Text>
            </TouchableOpacity>
          </View>
        ) : (
          <>
            {/* Step indicator */}
            <View style={styles.steps}>
              {STEPS.map((label, i) => (
                <View key={i} style={styles.stepItem}>
                  <View style={[styles.stepDot, i <= step && styles.stepDotActive]} />
                  <Text style={[styles.stepLabel, i === step && styles.stepLabelActive]}>{label}</Text>
                </View>
              ))}
            </View>

            {/* Confirmed info */}
            {item && (
              <View style={styles.infoCard}>
                <Text style={styles.label}>ITEM</Text>
                <Text style={styles.sku}>{item.sku}</Text>
                <Text style={styles.itemName}>{item.item_name}</Text>
                {locations.length > 0 && (
                  <View style={styles.locationList}>
                    {locations.map((loc, i) => (
                      <Text key={i} style={styles.locationText}>
                        {loc.bin_code}: {loc.quantity_on_hand} on hand
                      </Text>
                    ))}
                  </View>
                )}
              </View>
            )}

            {fromBin && (
              <View style={styles.infoCard}>
                <Text style={styles.label}>FROM BIN</Text>
                <Text style={styles.binValue}>{fromBin.bin_code}</Text>
                <Text style={styles.available}>Available: {fromBin.available}</Text>
              </View>
            )}

            {toBin && (
              <View style={styles.infoCard}>
                <Text style={styles.label}>TO BIN</Text>
                <Text style={styles.binValue}>{toBin.bin_code}</Text>
              </View>
            )}

            {/* Scan input for current step */}
            {step < 3 && (
              <ScanInput
                placeholder={STEPS[step]}
                onScan={handleScan}
                disabled={scanDisabled}
              />
            )}

            {/* Quantity + Confirm for step 3 */}
            {step === 3 && (
              <>
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
                  <Text style={styles.buttonPrimaryText}>CONFIRM TRANSFER</Text>
                </TouchableOpacity>
              </>
            )}
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
  steps: { flexDirection: 'row', justifyContent: 'space-between', marginBottom: 20 },
  stepItem: { alignItems: 'center', flex: 1 },
  stepDot: { width: 10, height: 10, borderRadius: 5, backgroundColor: colors.border, marginBottom: 4 },
  stepDotActive: { backgroundColor: colors.accentRed },
  stepLabel: { fontFamily: fonts.mono, fontSize: 9, color: colors.textMuted, letterSpacing: 0.3, textAlign: 'center' },
  stepLabelActive: { color: colors.accentRed, fontWeight: '700' },
  infoCard: {
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    padding: 12, marginBottom: 12,
  },
  label: { fontFamily: fonts.mono, fontSize: 10, fontWeight: '600', color: colors.textMuted, letterSpacing: 0.3, marginBottom: 2 },
  sku: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textPrimary },
  itemName: { fontSize: 13, color: colors.textMuted, marginTop: 2 },
  locationList: { marginTop: 8 },
  locationText: { fontFamily: fonts.mono, fontSize: 12, color: colors.textMuted, marginTop: 2 },
  binValue: { fontFamily: fonts.mono, fontSize: 16, fontWeight: '700', color: colors.textPrimary },
  available: { fontFamily: fonts.mono, fontSize: 12, color: colors.textMuted, marginTop: 2 },
  qtyRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 },
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
  successSection: { alignItems: 'center', paddingVertical: 32 },
  buttonSecondary: {
    backgroundColor: colors.background, borderWidth: 1.5, borderColor: colors.border, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonSecondaryText: { color: colors.textMuted, fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', letterSpacing: 0.5 },
  successSection: { alignItems: 'center', paddingVertical: 32 },
  successText: { fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.success, marginBottom: 8 },
  successDetail: { fontFamily: fonts.mono, fontSize: 13, color: colors.textMuted, marginBottom: 24, textAlign: 'center' },
});
