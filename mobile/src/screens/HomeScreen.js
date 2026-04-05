import React, { useState, useEffect, useCallback } from 'react';
import { View, Text, TouchableOpacity, ScrollView, StyleSheet, Alert } from 'react-native';
import { useFocusEffect } from '@react-navigation/native';
import { useAuth } from '../auth/AuthContext';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import ActiveBatchBanner from '../components/ActiveBatchBanner';
import WarehouseSelector from '../components/WarehouseSelector';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

const FUNCTIONS = [
  { key: 'receive', label: 'RECEIVE', screen: 'Receive', accent: 'red' },
  { key: 'pick', label: 'PICK', screen: 'PickScan', accent: 'red' },
  { key: 'pack_ship', label: 'PACK / SHIP', screen: 'PackShip', accent: 'copper' },
  { key: 'count', label: 'COUNT', screen: 'Count', accent: 'copper' },
  { key: 'transfer', label: 'TRANSFER', screen: 'Transfer', accent: 'gray' },
];

export default function HomeScreen({ navigation }) {
  const { user, warehouseId, logout, switchWarehouse } = useAuth();
  const [allowedFunctions, setAllowedFunctions] = useState([]);
  const [badges, setBadges] = useState({});
  const [activeBatch, setActiveBatch] = useState(null);
  const [batchDismissed, setBatchDismissed] = useState(false);
  const [warehouses, setWarehouses] = useState([]);
  const [warehouseCode, setWarehouseCode] = useState('');
  const [warehouseName, setWarehouseName] = useState('');
  const [showWarehousePicker, setShowWarehousePicker] = useState(false);
  const [error, setError] = useState('');
  const [scanDisabled, setScanDisabled] = useState(false);

  const loadData = useCallback(async () => {
    if (!warehouseId) return;

    try {
      const [meResp, dashResp, batchResp, whResp] = await Promise.all([
        client.get('/api/auth/me'),
        client.get(`/api/admin/dashboard?warehouse_id=${warehouseId}`),
        client.get('/api/picking/active-batch'),
        client.get('/api/warehouses/list'),
      ]);

      setAllowedFunctions(meResp.data.allowed_functions || []);

      const stats = dashResp.data;
      setBadges({
        receive: stats.to_receive || 0,
        pick: stats.to_pick || 0,
        pack_ship: stats.to_pack || 0,
        count: 0,
      });

      if (batchResp.data.active) {
        setActiveBatch(batchResp.data);
      } else {
        setActiveBatch(null);
      }

      const whList = whResp.data.warehouses || [];
      setWarehouses(whList);
      const current = whList.find((w) => w.id === warehouseId);
      if (current) {
        setWarehouseCode(current.code);
        setWarehouseName(current.name);
      }
    } catch {
      // Silent fail on refresh — data shows stale
    }
  }, [warehouseId, batchDismissed]);

  useFocusEffect(
    useCallback(() => {
      loadData();
    }, [loadData])
  );

  const handleScan = async (barcode) => {
    try {
      const itemResp = await client.get(`/api/lookup/item/${encodeURIComponent(barcode)}`);
      if (itemResp.data && itemResp.data.item) {
        const item = itemResp.data.item;
        const locations = (itemResp.data.locations || [])
          .map((l) => `${l.bin_code}: ${l.quantity_on_hand}`)
          .join('\n');
        Alert.alert(
          item.sku,
          `${item.item_name}\n\n${locations || 'No stock on hand'}`
        );
        return;
      }
    } catch {
      // Not an item, try bin
    }

    try {
      const binResp = await client.get(`/api/lookup/bin/${encodeURIComponent(barcode)}`);
      if (binResp.data && binResp.data.bin) {
        const bin = binResp.data.bin;
        const contents = (binResp.data.contents || [])
          .map((c) => `${c.sku}: ${c.quantity_on_hand}`)
          .join('\n');
        Alert.alert(
          bin.bin_code,
          `${bin.bin_type}\n\n${contents || 'Empty bin'}`
        );
        return;
      }
    } catch {
      // Not a bin either
    }

    setError('Barcode not recognized');
    setScanDisabled(true);
  };

  const visibleFunctions = FUNCTIONS.filter(
    (fn) => allowedFunctions.includes(fn.key)
  );

  const getBadgeCount = (key) => badges[key] || 0;

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <Text style={styles.headerLogo}>SENTRY</Text>
        <TouchableOpacity onPress={() => setShowWarehousePicker(true)}>
          <Text style={styles.headerWarehouse}>{warehouseCode || '---'}</Text>
        </TouchableOpacity>
      </View>

      <ScrollView style={styles.content} contentContainerStyle={styles.contentInner}>
        <ScanInput
          placeholder="SCAN BARCODE"
          onScan={handleScan}
          disabled={scanDisabled}
        />

        {activeBatch && !batchDismissed && (
          <ActiveBatchBanner
            batch={activeBatch}
            onResume={() => navigation.navigate('PickWalk', { batch_id: activeBatch.batch_id })}
            onDismiss={() => setBatchDismissed(true)}
          />
        )}

        {visibleFunctions.map((fn) => (
          <TouchableOpacity
            key={fn.key}
            style={[
              styles.functionRow,
              fn.accent === 'red' && styles.functionRowRed,
              fn.accent === 'copper' && styles.functionRowCopper,
              fn.accent === 'gray' && styles.functionRowGray,
            ]}
            onPress={() => navigation.navigate(fn.screen)}
          >
            <Text style={styles.functionLabel}>{fn.label}</Text>
            {fn.accent !== 'gray' && getBadgeCount(fn.key) > 0 && (
              <View style={[styles.badge, fn.accent === 'copper' && styles.badgeCopper]}>
                <Text style={styles.badgeText}>{getBadgeCount(fn.key)}</Text>
              </View>
            )}
          </TouchableOpacity>
        ))}
      </ScrollView>

      <View style={styles.footer}>
        <Text style={styles.footerText}>v0.9.0 — {warehouseName}</Text>
      </View>

      <ErrorPopup
        visible={!!error}
        message={error}
        onDismiss={() => {
          setError('');
          setScanDisabled(false);
        }}
      />

      <WarehouseSelector
        visible={showWarehousePicker}
        warehouses={warehouses}
        selected={warehouseId}
        onSelect={(id) => {
          switchWarehouse(id);
          setShowWarehousePicker(false);
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.background,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingTop: 52,
    paddingBottom: 12,
    borderBottomWidth: 2,
    borderBottomColor: colors.accentRed,
  },
  headerLogo: {
    fontFamily: fonts.mono,
    fontSize: 18,
    fontWeight: '700',
    color: colors.accentRed,
    letterSpacing: 2,
  },
  headerWarehouse: {
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '600',
    color: colors.textPrimary,
    letterSpacing: 0.3,
  },
  content: {
    flex: 1,
  },
  contentInner: {
    padding: 16,
  },
  functionRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    backgroundColor: colors.background,
    borderWidth: 1,
    borderColor: colors.border,
    borderLeftWidth: 3,
    borderRadius: 8,
    paddingVertical: 16,
    paddingHorizontal: 16,
    marginBottom: 8,
    minHeight: 48,
  },
  functionRowRed: {
    borderLeftColor: colors.accentRed,
  },
  functionRowCopper: {
    borderLeftColor: colors.copper,
  },
  functionRowGray: {
    borderLeftColor: colors.border,
  },
  functionLabel: {
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    color: colors.textPrimary,
    letterSpacing: 0.5,
  },
  badge: {
    backgroundColor: colors.accentRed,
    borderRadius: 10,
    paddingHorizontal: 8,
    paddingVertical: 2,
    minWidth: 24,
    alignItems: 'center',
  },
  badgeCopper: {
    backgroundColor: colors.copper,
  },
  badgeText: {
    color: '#FFFFFF',
    fontFamily: fonts.mono,
    fontSize: 12,
    fontWeight: '700',
  },
  footer: {
    paddingVertical: 12,
    alignItems: 'center',
  },
  footerText: {
    fontFamily: fonts.mono,
    fontSize: 11,
    color: colors.textMuted,
  },
});
