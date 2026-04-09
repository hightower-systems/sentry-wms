import React, { useState } from 'react';
import { View, Text, TouchableOpacity, ActivityIndicator, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import PagedList from '../components/PagedList';
import useScreenError from '../hooks/useScreenError';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import ScreenHeader from '../components/ScreenHeader';
import { colors, fonts, radii, screenStyles, buttonStyles, listStyles } from '../theme/styles';

export default function PickScanScreen({ navigation }) {
  const { warehouseId } = useAuth();
  const [orders, setOrders] = useState([]);
  const { error, scanDisabled, showError, clearError } = useScreenError();
  const [loading, setLoading] = useState(false);

  const handleScan = async (barcode) => {
    console.log('[SCAN_DEBUG] PickScanScreen.handleScan received:', JSON.stringify(barcode));
    // Client-side duplicate check
    if (orders.find((o) => o.so_barcode === barcode || o.so_number === barcode)) {
      showError('Already scanned');
      return;
    }

    try {
      const resp = await client.post('/api/picking/wave-validate', {
        so_barcode: barcode,
        warehouse_id: warehouseId,
      });
      if (resp.data.valid) {
        setOrders((prev) => [...prev, {
          so_id: resp.data.so_id,
          so_number: resp.data.so_number,
          so_barcode: barcode,
          item_count: resp.data.line_count || resp.data.item_count || 0,
          unit_count: resp.data.total_units || resp.data.unit_count || 0,
        }]);
      }
    } catch (err) {
      const data = err.response?.data;
      if (err.response?.status === 409) {
        showError(data?.error || `Order already in batch #${data?.batch_id}`);
      } else if (err.response?.status === 404) {
        showError('Order not found');
      } else {
        showError(data?.error || 'Validation failed');
      }
    }
  };

  const removeOrder = (so_id) => {
    setOrders((prev) => prev.filter((o) => o.so_id !== so_id));
  };

  const handleLoadAll = async () => {
    if (orders.length === 0) return;
    setLoading(true);
    try {
      const resp = await client.post('/api/picking/wave-create', {
        so_ids: orders.map((o) => o.so_id),
        warehouse_id: warehouseId,
      });
      navigation.replace('PickWalk', {
        batch_id: resp.data.batch_id,
        batch: resp.data,
      });
    } catch (err) {
      showError(err.response?.data?.error || 'Failed to create batch');
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <View style={styles.loadingScreen}>
        <ActivityIndicator size="large" color={colors.accentRed} />
        <Text style={styles.loadingText}>
          Building pick path for {orders.length} order{orders.length !== 1 ? 's' : ''}...
        </Text>
      </View>
    );
  }

  return (
    <View style={screenStyles.screen}>
      <ScreenHeader
        title="PICK ORDERS"
        onBack={() => navigation.goBack()}
        right={
          orders.length > 0 ? (
            <View style={styles.badge}>
              <Text style={styles.badgeText}>{orders.length}</Text>
            </View>
          ) : undefined
        }
      />

      <View style={screenStyles.content}>
        <View style={{ padding: 16, paddingBottom: 0 }}>
          <ScanInput placeholder="SCAN SO" onScan={handleScan} disabled={scanDisabled} />
        </View>

        <View style={{ flex: 1, paddingHorizontal: 16 }}>
          <PagedList
            items={orders}
            pageSize={20}
            renderItem={(order) => (
              <View style={[listStyles.row, { padding: 14 }]}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.soNumber}>{order.so_number}</Text>
                  <Text style={styles.orderDetail}>
                    {order.item_count} item{order.item_count !== 1 ? 's' : ''} · {order.unit_count} unit{order.unit_count !== 1 ? 's' : ''}
                  </Text>
                </View>
                <TouchableOpacity
                  style={listStyles.removeBtn}
                  onPress={() => removeOrder(order.so_id)}
                >
                  <Text style={listStyles.removeText}>X</Text>
                </TouchableOpacity>
              </View>
            )}
          />
        </View>

        <View style={screenStyles.bottomBar}>
          <TouchableOpacity
            style={[buttonStyles.buttonPrimary, { flex: 1 }, orders.length === 0 && buttonStyles.buttonDisabled]}
            onPress={handleLoadAll}
            disabled={orders.length === 0}
          >
            <Text style={buttonStyles.buttonPrimaryText}>LOAD ALL ORDERS</Text>
          </TouchableOpacity>
        </View>
      </View>

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
  soNumber: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  orderDetail: { fontSize: 12, color: colors.textMuted, marginTop: 2 },
  loadingScreen: { flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: colors.background, padding: 32 },
  loadingText: { fontFamily: fonts.mono, fontSize: 14, color: colors.textMuted, marginTop: 16, textAlign: 'center' },
});
