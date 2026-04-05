import React, { useState } from 'react';
import { View, Text, TouchableOpacity, ActivityIndicator, StyleSheet } from 'react-native';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import PagedList from '../components/PagedList';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

export default function PickScanScreen({ navigation }) {
  const { warehouseId } = useAuth();
  const [orders, setOrders] = useState([]);
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);
  const [loading, setLoading] = useState(false);

  const handleScan = async (barcode) => {
    // Client-side duplicate check
    if (orders.find((o) => o.so_barcode === barcode || o.so_number === barcode)) {
      setError('Already scanned');
      setScanDisabled(true);
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
          item_count: resp.data.item_count || 0,
          unit_count: resp.data.unit_count || 0,
        }]);
      }
    } catch (err) {
      const data = err.response?.data;
      if (err.response?.status === 409) {
        setError(data?.error || `Order already in batch #${data?.batch_id}`);
      } else if (err.response?.status === 404) {
        setError('Order not found');
      } else {
        setError(data?.error || 'Validation failed');
      }
      setScanDisabled(true);
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
      setError(err.response?.data?.error || 'Failed to create batch');
      setScanDisabled(true);
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
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity style={styles.backBtn} onPress={() => navigation.goBack()}>
          <Text style={styles.backText}>{'<'}</Text>
        </TouchableOpacity>
        <Text style={styles.headerTitle}>PICK ORDERS</Text>
        {orders.length > 0 && (
          <View style={styles.badge}>
            <Text style={styles.badgeText}>{orders.length}</Text>
          </View>
        )}
      </View>

      <View style={styles.content}>
        <View style={{ padding: 16, paddingBottom: 0 }}>
          <ScanInput placeholder="SCAN SO" onScan={handleScan} disabled={scanDisabled} />
        </View>

        <View style={{ flex: 1, paddingHorizontal: 16 }}>
          <PagedList
            items={orders}
            pageSize={20}
            renderItem={(order) => (
              <View style={styles.orderRow}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.soNumber}>{order.so_number}</Text>
                  <Text style={styles.orderDetail}>
                    {order.item_count} item{order.item_count !== 1 ? 's' : ''} · {order.unit_count} unit{order.unit_count !== 1 ? 's' : ''}
                  </Text>
                </View>
                <TouchableOpacity
                  style={styles.removeBtn}
                  onPress={() => removeOrder(order.so_id)}
                >
                  <Text style={styles.removeText}>X</Text>
                </TouchableOpacity>
              </View>
            )}
          />
        </View>

        <View style={styles.bottomBar}>
          <TouchableOpacity
            style={[styles.buttonPrimary, orders.length === 0 && styles.buttonDisabled]}
            onPress={handleLoadAll}
            disabled={orders.length === 0}
          >
            <Text style={styles.buttonPrimaryText}>LOAD ALL ORDERS</Text>
          </TouchableOpacity>
        </View>
      </View>

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
  orderRow: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    borderWidth: 1, borderColor: colors.border, borderRadius: 8,
    padding: 14, marginBottom: 8, minHeight: 48,
  },
  soNumber: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textPrimary },
  orderDetail: { fontSize: 12, color: colors.textMuted, marginTop: 2 },
  removeBtn: { padding: 8, minWidth: 48, minHeight: 48, alignItems: 'center', justifyContent: 'center' },
  removeText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textMuted },
  bottomBar: { padding: 16, borderTopWidth: 1, borderTopColor: colors.border },
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: 8,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonPrimaryText: { color: colors.cream, fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', letterSpacing: 0.5 },
  buttonDisabled: { opacity: 0.5 },
  loadingScreen: { flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: colors.background, padding: 32 },
  loadingText: { fontFamily: fonts.mono, fontSize: 14, color: colors.textMuted, marginTop: 16, textAlign: 'center' },
});
