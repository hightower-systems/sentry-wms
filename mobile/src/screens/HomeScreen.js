import React, { useState, useEffect, useCallback, useRef } from 'react';
import { View, Text, TouchableOpacity, ScrollView, Modal, Pressable, ActivityIndicator, StyleSheet, TextInput } from 'react-native';
import { useFocusEffect, useScrollToTop } from '@react-navigation/native';
import { useAuth } from '../auth/AuthContext';
import { useScanSettingsContext } from '../context/ScanSettingsContext';
import ScanInput from '../components/ScanInput';
import ErrorPopup from '../components/ErrorPopup';
import useScreenError from '../hooks/useScreenError';
import ActiveBatchBanner from '../components/ActiveBatchBanner';
import WarehouseSelector from '../components/WarehouseSelector';
import client, { getStoredApiUrl, setApiUrl } from '../api/client';
import { colors, fonts, radii, spacing } from '../theme/styles';

const FUNCTIONS = [
  { key: 'pick', label: 'PICK', sub: 'Pick orders', screen: 'PickScan', accent: 'red' },
  { key: 'pack', label: 'PACK', sub: 'Verify & pack', screen: 'Pack', accent: 'red' },
  { key: 'receive', label: 'RECEIVE', sub: 'PO receiving', screen: 'Receive', accent: 'copper' },
  { key: 'putaway', label: 'PUT-AWAY', sub: 'Bin placement', screen: 'PutAway', accent: 'copper' },
  { key: 'transfer', label: 'TRANSFER', sub: 'Bin to bin', screen: 'Transfer', accent: 'gray' },
  { key: 'count', label: 'COUNT', sub: 'Cycle count', screen: 'Count', accent: 'gray' },
  { key: 'ship', label: 'SHIP', sub: 'Fulfill & ship', screen: 'Ship', accent: 'gray' },
];

const ACCENT_COLORS = {
  red: colors.accentRed,
  copper: colors.copper,
  gray: colors.grayAccent,
};

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
  const [needsWarehouse, setNeedsWarehouse] = useState(false);
  const warehouseResolved = useRef(false);
  const [requirePacking, setRequirePacking] = useState(true);
  const { error, scanDisabled, showError, clearError } = useScreenError();
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [showScanConfig, setShowScanConfig] = useState(false);
  const [showChangePw, setShowChangePw] = useState(false);
  const [pwForm, setPwForm] = useState({ current: '', newPw: '', confirm: '' });
  const [pwError, setPwError] = useState('');
  const [pwSuccess, setPwSuccess] = useState('');
  const [pwSaving, setPwSaving] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const scanSettings = useScanSettingsContext();
  const [serverUrl, setServerUrl] = useState('');
  const scrollRef = React.useRef(null);
  useScrollToTop(scrollRef);
  // Modal state for replacing Alert.alert
  const [infoModal, setInfoModal] = useState({ visible: false, title: '', message: '' });
  const [confirmModal, setConfirmModal] = useState({ visible: false, title: '', message: '', onConfirm: null, confirmText: 'OK', confirmDestructive: false });

  // On first load after login, if no warehouse is set, fetch the list and
  // either auto-select (single warehouse) or show a blocking picker.
  useEffect(() => {
    if (warehouseId || warehouseResolved.current) return;
    (async () => {
      try {
        const resp = await client.get('/api/warehouses/list');
        const list = resp.data.warehouses || [];
        setWarehouses(list);
        if (list.length === 1) {
          switchWarehouse(list[0].id);
          warehouseResolved.current = true;
        } else if (list.length > 1) {
          setNeedsWarehouse(true);
        }
      } catch {
        // If fetch fails, show picker with empty list - user can retry via header pill
        setNeedsWarehouse(true);
      }
    })();
  }, [warehouseId, switchWarehouse]);

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
      setRequirePacking(meResp.data.require_packing !== false);

      const stats = dashResp.data;
      setBadges({
        receive: stats.pending_receipts || 0,
        putaway: stats.items_awaiting_putaway || 0,
        pick: stats.orders_ready_to_pick || 0,
        pack: stats.ready_to_pack || 0,
        ship: stats.ready_to_ship || 0,
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
      // Silent fail on refresh - data shows stale
    } finally {
      setInitialLoading(false);
    }
  }, [warehouseId]);

  useFocusEffect(
    useCallback(() => {
      setBatchDismissed(false);
      loadData();
      getStoredApiUrl().then(setServerUrl);
    }, [loadData])
  );

  const handleScan = async (barcode) => {
    const cleaned = barcode.replace(/[\r\n\s]+/g, '').trim();
    if (!cleaned) return;
    const encoded = encodeURIComponent(cleaned);

    // Try item lookup (UPC or SKU)
    try {
      const itemResp = await client.get(`/api/lookup/item/${encoded}`);
      if (itemResp.data && itemResp.data.item) {
        const item = itemResp.data.item;
        const locations = (itemResp.data.locations || [])
          .map((l) => `${l.bin_code}: ${l.quantity_on_hand}`)
          .join('\n');
        setInfoModal({ visible: true, title: item.sku, message: `${item.item_name}\n\n${locations || 'No stock on hand'}` });
        return;
      }
    } catch {
      // Not an item
    }

    // Try bin lookup
    try {
      const binResp = await client.get(`/api/lookup/bin/${encoded}`);
      if (binResp.data && binResp.data.bin) {
        const bin = binResp.data.bin;
        const contents = (binResp.data.items || [])
          .map((c) => `${c.sku}: ${c.quantity_on_hand}`)
          .join('\n');
        setInfoModal({ visible: true, title: bin.bin_code, message: `${bin.bin_type}\n\n${contents || 'Empty bin'}` });
        return;
      }
    } catch {
      // Not a bin
    }

    // Try PO lookup
    try {
      const poResp = await client.get(`/api/receiving/po/${encoded}`);
      if (poResp.data && poResp.data.purchase_order) {
        const po = poResp.data.purchase_order;
        navigation.navigate('Receive', { po_number: po.po_number });
        return;
      }
    } catch {
      // Not a PO
    }

    // Try SO lookup  -  generic first to check status, then route appropriately
    try {
      const soResp = await client.get(`/api/lookup/so/${encoded}`);
      if (soResp.data && soResp.data.sales_order) {
        const so = soResp.data.sales_order;
        if (so.status === 'PACKED') {
          navigation.navigate('Ship', { so_number: so.so_number });
          return;
        }
        if (so.status === 'PICKED') {
          navigation.navigate('Ship', { so_number: so.so_number });
          return;
        }
        // SO exists but not in actionable status  -  show info
        const infoLines = [so.customer_name, so.customer_phone, `Status: ${so.status}`].filter(Boolean);
        setInfoModal({ visible: true, title: so.so_number, message: infoLines.join('\n') });
        return;
      }
    } catch {
      // Not an SO
    }

    showError('Barcode not recognized');
  };

  const visibleFunctions = FUNCTIONS.filter(
    (fn) => allowedFunctions.includes(fn.key)
  );

  const getBadgeCount = (key) => badges[key] || 0;

  const userInitial = (user?.full_name || user?.username || 'U').charAt(0).toUpperCase();

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <Text style={styles.headerLogo}>SENTRY</Text>
        <View style={styles.headerRight}>
          <TouchableOpacity style={styles.warehousePill} onPress={() => setShowWarehousePicker(true)}>
            <Text style={styles.warehousePillText}>{warehouseCode || '---'}</Text>
          </TouchableOpacity>
          <TouchableOpacity style={styles.userAvatar} onPress={() => setShowUserMenu(true)}>
            <Text style={styles.userAvatarText}>{userInitial}</Text>
          </TouchableOpacity>
        </View>
      </View>

      <Modal visible={showUserMenu} transparent animationType="fade">
        <Pressable style={styles.menuOverlay} onPress={() => setShowUserMenu(false)}>
          <View style={styles.menuCard}>
            <Text style={styles.menuUser}>{user?.full_name || user?.username || 'User'}</Text>
            <Text style={styles.menuRole}>{user?.role}</Text>
            <View style={styles.menuDivider} />
            <TouchableOpacity style={styles.menuItem} onPress={() => {
              setShowUserMenu(false);
              getStoredApiUrl().then(setServerUrl);
              setShowScanConfig(true);
            }}>
              <Text style={styles.menuItemText}>SETTINGS</Text>
            </TouchableOpacity>
            <TouchableOpacity style={styles.menuItem} onPress={() => {
              setShowUserMenu(false);
              setPwForm({ current: '', newPw: '', confirm: '' });
              setPwError('');
              setPwSuccess('');
              setShowChangePw(true);
            }}>
              <Text style={styles.menuItemText}>CHANGE PASSWORD</Text>
            </TouchableOpacity>
            <TouchableOpacity style={styles.menuItem} onPress={() => { setShowUserMenu(false); logout(); }}>
              <Text style={styles.menuItemTextDanger}>LOGOUT</Text>
            </TouchableOpacity>
          </View>
        </Pressable>
      </Modal>

      {/* Scan Settings Modal */}
      <Modal visible={showScanConfig} transparent animationType="fade">
        <Pressable style={styles.menuOverlay} onPress={() => setShowScanConfig(false)}>
          <Pressable style={[styles.scanConfigCard, { maxHeight: '80%' }]} onPress={() => {}}>
            <ScrollView keyboardShouldPersistTaps="handled" showsVerticalScrollIndicator={false}>
            <Text style={styles.scanConfigTitle}>SETTINGS</Text>

            <Text style={styles.scanConfigLabel}>SERVER URL</Text>
            <TextInput
              style={styles.scanConfigInput}
              value={serverUrl}
              onChangeText={setServerUrl}
              onBlur={() => { if (serverUrl.trim()) setApiUrl(serverUrl.trim()); }}
              placeholder="http://10.1.10.150:5000"
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              placeholderTextColor={colors.textPlaceholder}
            />
            <Text style={[styles.scanConfigHint, { marginBottom: 16 }]}>API server address  -  change requires re-login</Text>

            <Text style={styles.scanConfigLabel}>SCAN MODE</Text>
            <View style={styles.scanModeRow}>
              <TouchableOpacity
                style={[styles.scanModeBtn, scanSettings?.mode === 'keyboard' && styles.scanModeBtnActive]}
                onPress={() => scanSettings?.setMode('keyboard')}
              >
                <Text style={[styles.scanModeBtnText, scanSettings?.mode === 'keyboard' && styles.scanModeBtnTextActive]}>KEYBOARD</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[styles.scanModeBtn, scanSettings?.mode === 'intent' && styles.scanModeBtnActive]}
                onPress={() => scanSettings?.setMode('intent')}
              >
                <Text style={[styles.scanModeBtnText, scanSettings?.mode === 'intent' && styles.scanModeBtnTextActive]}>INTENT</Text>
              </TouchableOpacity>
            </View>
            <Text style={styles.scanConfigHint}>
              {scanSettings?.mode === 'keyboard'
                ? 'Scanner types into focused text field (default)'
                : 'Scanner sends broadcast intent (Chainway native)'}
            </Text>

            {scanSettings?.mode === 'intent' && (
              <>
                <Text style={[styles.scanConfigLabel, { marginTop: 16 }]}>INTENT ACTION</Text>
                <TextInput
                  style={styles.scanConfigInput}
                  value={scanSettings.intentAction}
                  onChangeText={scanSettings.setIntentAction}
                  autoCapitalize="none"
                  autoCorrect={false}
                  placeholderTextColor={colors.textPlaceholder}
                />
                <Text style={[styles.scanConfigLabel, { marginTop: 8 }]}>EXTRA KEY</Text>
                <TextInput
                  style={styles.scanConfigInput}
                  value={scanSettings.intentExtra}
                  onChangeText={scanSettings.setIntentExtra}
                  autoCapitalize="none"
                  autoCorrect={false}
                  placeholderTextColor={colors.textPlaceholder}
                />
                {!scanSettings.scannerAvailable && (
                  <Text style={styles.scanConfigWarn}>Native module not available  -  intent mode requires a standalone APK build</Text>
                )}
              </>
            )}

            <TouchableOpacity style={styles.scanConfigDone} onPress={() => {
              if (serverUrl.trim()) setApiUrl(serverUrl.trim());
              setShowScanConfig(false);
            }}>
              <Text style={styles.scanConfigDoneText}>DONE</Text>
            </TouchableOpacity>
            </ScrollView>
          </Pressable>
        </Pressable>
      </Modal>

      {/* Change Password Modal */}
      <Modal visible={showChangePw} transparent animationType="fade">
        <Pressable style={styles.menuOverlay} onPress={() => setShowChangePw(false)}>
          <Pressable style={styles.infoModalCard} onPress={() => {}}>
            <Text style={styles.infoModalTitle}>CHANGE PASSWORD</Text>
            <TextInput
              style={[styles.scanConfigInput, { marginBottom: 10 }]}
              placeholder="Current password"
              placeholderTextColor={colors.textPlaceholder}
              value={pwForm.current}
              onChangeText={(t) => { setPwForm({ ...pwForm, current: t }); setPwError(''); setPwSuccess(''); }}
              secureTextEntry
              autoCapitalize="none"
            />
            <TextInput
              style={[styles.scanConfigInput, { marginBottom: 10 }]}
              placeholder="New password"
              placeholderTextColor={colors.textPlaceholder}
              value={pwForm.newPw}
              onChangeText={(t) => { setPwForm({ ...pwForm, newPw: t }); setPwError(''); setPwSuccess(''); }}
              secureTextEntry
              autoCapitalize="none"
            />
            <TextInput
              style={[styles.scanConfigInput, { marginBottom: 10 }]}
              placeholder="Confirm new password"
              placeholderTextColor={colors.textPlaceholder}
              value={pwForm.confirm}
              onChangeText={(t) => { setPwForm({ ...pwForm, confirm: t }); setPwError(''); setPwSuccess(''); }}
              secureTextEntry
              autoCapitalize="none"
            />
            {pwError ? <Text style={{ color: colors.accentRed, fontSize: 12, marginBottom: 8 }}>{pwError}</Text> : null}
            {pwSuccess ? <Text style={{ color: colors.copper, fontSize: 12, marginBottom: 8 }}>{pwSuccess}</Text> : null}
            <View style={{ flexDirection: 'row', gap: 8, marginTop: 4 }}>
              <TouchableOpacity
                style={[styles.infoModalButton, { flex: 1, marginTop: 0 }]}
                disabled={pwSaving}
                onPress={async () => {
                  if (!pwForm.current || !pwForm.newPw || !pwForm.confirm) {
                    setPwError('All fields are required');
                    return;
                  }
                  if (pwForm.newPw !== pwForm.confirm) {
                    setPwError('New passwords do not match');
                    return;
                  }
                  setPwSaving(true);
                  setPwError('');
                  try {
                    await client.post('/api/auth/change-password', {
                      current_password: pwForm.current,
                      new_password: pwForm.newPw,
                    });
                    setPwSuccess('Password changed - please log in again');
                    setTimeout(() => { setShowChangePw(false); logout(); }, 1500);
                  } catch (err) {
                    setPwError(err.response?.data?.error || 'Failed to change password');
                  } finally {
                    setPwSaving(false);
                  }
                }}
              >
                <Text style={styles.infoModalButtonText}>{pwSaving ? 'SAVING...' : 'CHANGE'}</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[styles.infoModalButton, { flex: 1, marginTop: 0, backgroundColor: colors.cardBorder }]}
                onPress={() => setShowChangePw(false)}
              >
                <Text style={[styles.infoModalButtonText, { color: colors.textPrimary }]}>CANCEL</Text>
              </TouchableOpacity>
            </View>
          </Pressable>
        </Pressable>
      </Modal>

      <ScrollView ref={scrollRef} style={styles.content} contentContainerStyle={styles.contentInner} keyboardShouldPersistTaps="handled">
        <ScanInput
          placeholder="SCAN BARCODE"
          onScan={handleScan}
          disabled={scanDisabled}
        />

        {activeBatch && !batchDismissed && (
          <ActiveBatchBanner
            batch={activeBatch}
            onResume={() => navigation.navigate('PickWalk', { batch_id: activeBatch.batch_id })}
            onDismiss={() => {
              setConfirmModal({
                visible: true,
                title: 'Dismiss Batch',
                message: 'This batch will reappear next time you return to this screen. Resume it later from here.',
                confirmText: 'Dismiss',
                confirmDestructive: false,
                onConfirm: () => { setConfirmModal((p) => ({ ...p, visible: false })); setBatchDismissed(true); },
              });
            }}
            onDelete={() => {
              setConfirmModal({
                visible: true,
                title: 'Delete Batch',
                message: 'Cancel this batch and release all allocated inventory? Orders will return to OPEN status.',
                confirmText: 'Delete',
                confirmDestructive: true,
                onConfirm: async () => {
                  setConfirmModal((p) => ({ ...p, visible: false }));
                  try {
                    await client.post('/api/picking/cancel-batch', { batch_id: activeBatch.batch_id });
                    setActiveBatch(null);
                    loadData();
                  } catch {
                    showError('Failed to cancel batch');
                  }
                },
              });
            }}
          />
        )}

        <Text style={styles.operationsLabel}>OPERATIONS</Text>

        {initialLoading ? (
          <ActivityIndicator size="large" color={colors.accentRed} style={{ marginTop: 32 }} />
        ) : (
        <View style={styles.grid}>
          {visibleFunctions.map((fn, index) => {
            const accentColor = ACCENT_COLORS[fn.accent];
            const badgeCount = getBadgeCount(fn.key);
            const isShip = fn.key === 'ship';

            return (
              <TouchableOpacity
                key={fn.key}
                style={[styles.gridCard, isShip && styles.gridCardFull]}
                onPress={() => navigation.navigate(fn.screen)}
                activeOpacity={0.7}
              >
                <View style={[styles.accentStripe, { backgroundColor: accentColor }]} />
                <View style={[styles.accentDash, { backgroundColor: accentColor }]} />
                <Text style={styles.cardLabel}>{fn.label}</Text>
                <Text style={styles.cardSub}>{fn.sub}</Text>
              </TouchableOpacity>
            );
          })}
        </View>
        )}
      </ScrollView>

      <View style={styles.footer}>
        <TouchableOpacity onPress={() => { getStoredApiUrl().then(setServerUrl); setShowScanConfig(true); }}>
          <Text style={styles.footerIp}>{serverUrl || 'Set Server URL'}</Text>
        </TouchableOpacity>
        <Text style={styles.footerText}>v1.4.1 / {warehouseName}</Text>
      </View>

      {/* Info modal (replaces Alert.alert for lookups) */}
      <Modal visible={infoModal.visible} transparent animationType="fade">
        <Pressable style={styles.menuOverlay} onPress={() => setInfoModal({ visible: false, title: '', message: '' })}>
          <Pressable style={styles.infoModalCard} onPress={() => {}}>
            <Text style={styles.infoModalTitle}>{infoModal.title}</Text>
            <Text style={styles.infoModalMessage}>{infoModal.message}</Text>
            <TouchableOpacity
              style={styles.infoModalButton}
              onPress={() => setInfoModal({ visible: false, title: '', message: '' })}
            >
              <Text style={styles.infoModalButtonText}>OK</Text>
            </TouchableOpacity>
          </Pressable>
        </Pressable>
      </Modal>

      {/* Confirm modal (replaces Alert.alert for confirmations) */}
      <Modal visible={confirmModal.visible} transparent animationType="fade">
        <Pressable style={styles.menuOverlay} onPress={() => setConfirmModal((p) => ({ ...p, visible: false }))}>
          <Pressable style={styles.infoModalCard} onPress={() => {}}>
            <Text style={styles.infoModalTitle}>{confirmModal.title}</Text>
            <Text style={styles.infoModalMessage}>{confirmModal.message}</Text>
            <View style={{ flexDirection: 'row', gap: 8, marginTop: 16 }}>
              <TouchableOpacity
                style={[styles.infoModalButton, { flex: 1, backgroundColor: confirmModal.confirmDestructive ? colors.accentRed : colors.accentRed }]}
                onPress={confirmModal.onConfirm}
              >
                <Text style={styles.infoModalButtonText}>{confirmModal.confirmText}</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[styles.infoModalButton, { flex: 1, backgroundColor: colors.cardBorder }]}
                onPress={() => setConfirmModal((p) => ({ ...p, visible: false }))}
              >
                <Text style={[styles.infoModalButtonText, { color: colors.textPrimary }]}>Cancel</Text>
              </TouchableOpacity>
            </View>
          </Pressable>
        </Pressable>
      </Modal>

      <ErrorPopup
        visible={!!error}
        message={error}
        onDismiss={clearError}
      />

      <WarehouseSelector
        visible={showWarehousePicker || needsWarehouse}
        warehouses={warehouses}
        selected={warehouseId}
        onSelect={(id) => {
          switchWarehouse(id);
          setShowWarehousePicker(false);
          setNeedsWarehouse(false);
          warehouseResolved.current = true;
        }}
        onClose={needsWarehouse ? undefined : () => setShowWarehousePicker(false)}
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
  },
  headerLogo: {
    fontFamily: fonts.mono,
    fontSize: 18,
    fontWeight: '700',
    color: colors.accentRed,
    letterSpacing: 4,
  },
  headerRight: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
  },
  warehousePill: {
    backgroundColor: colors.cardBg,
    borderWidth: 1,
    borderColor: colors.cardBorder,
    borderRadius: radii.badge,
    paddingHorizontal: 12,
    paddingVertical: 8,
    minHeight: 32,
    justifyContent: 'center',
  },
  warehousePillText: {
    fontFamily: fonts.mono,
    fontSize: 9,
    fontWeight: '600',
    color: colors.textPrimary,
  },
  userAvatar: {
    backgroundColor: colors.accentRed,
    borderRadius: 8,
    width: 32,
    height: 32,
    alignItems: 'center',
    justifyContent: 'center',
  },
  userAvatarText: {
    color: colors.background,
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
  },
  menuOverlay: {
    flex: 1,
    backgroundColor: colors.overlay,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 24,
  },
  menuCard: {
    backgroundColor: colors.background,
    borderRadius: radii.card,
    padding: 16,
    minWidth: 180,
    borderWidth: 1,
    borderColor: colors.cardBorder,
  },
  menuUser: {
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  menuRole: {
    fontFamily: fonts.mono,
    fontSize: 11,
    color: colors.textMuted,
    marginTop: 2,
  },
  menuDivider: {
    height: 1,
    backgroundColor: colors.cardBorder,
    marginVertical: 12,
  },
  menuItem: {
    paddingVertical: 8,
  },
  menuItemText: {
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '600',
    color: colors.textPrimary,
    letterSpacing: 0.3,
  },
  menuItemTextDanger: {
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '600',
    color: colors.accentRed,
    letterSpacing: 0.3,
  },
  content: {
    flex: 1,
  },
  contentInner: {
    padding: 16,
    paddingBottom: 48,
  },
  operationsLabel: {
    fontFamily: fonts.mono,
    fontSize: 9,
    fontWeight: '600',
    color: colors.textMuted,
    letterSpacing: 2,
    marginBottom: 8,
    marginTop: 4,
  },
  grid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: spacing.cardGap,
  },
  gridCard: {
    backgroundColor: colors.cardBg,
    borderWidth: 1,
    borderColor: colors.cardBorder,
    borderRadius: radii.card,
    padding: spacing.cardPadding,
    paddingTop: 18,
    overflow: 'hidden',
    width: '48.5%',
  },
  gridCardFull: {
    width: '100%',
  },
  accentStripe: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    height: 5,
    borderTopLeftRadius: radii.card,
    borderTopRightRadius: radii.card,
  },
  accentDash: {
    width: 18,
    height: 2,
    borderRadius: 1,
    marginBottom: 6,
  },
  cardLabel: {
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    color: colors.textPrimary,
    letterSpacing: 0.5,
  },
  cardSub: {
    fontFamily: fonts.mono,
    fontSize: 11,
    color: colors.textMuted,
    marginTop: 2,
  },
  cardBadge: {
    position: 'absolute',
    top: 8,
    right: 8,
    borderRadius: 10,
    paddingHorizontal: 7,
    paddingVertical: 2,
    minWidth: 22,
    alignItems: 'center',
  },
  cardBadgeText: {
    color: colors.cream,
    fontFamily: fonts.mono,
    fontSize: 10,
    fontWeight: '700',
  },
  footer: {
    position: 'absolute',
    bottom: 0,
    left: 0,
    right: 0,
    paddingVertical: 12,
    alignItems: 'center',
    backgroundColor: colors.background,
  },
  footerIp: {
    fontFamily: fonts.mono,
    fontSize: 9,
    color: colors.textMuted,
    textDecorationLine: 'underline',
    marginBottom: 2,
  },
  footerText: {
    fontFamily: fonts.mono,
    fontSize: 9,
    color: colors.textPlaceholder,
  },
  // Scan config modal
  scanConfigCard: {
    backgroundColor: colors.background,
    borderRadius: radii.card,
    padding: 20,
    width: '90%',
    maxWidth: 340,
    borderWidth: 1,
    borderColor: colors.cardBorder,
    alignSelf: 'center',
    marginTop: 120,
  },
  scanConfigTitle: {
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    color: colors.textPrimary,
    letterSpacing: 0.5,
    marginBottom: 16,
  },
  scanConfigLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    fontWeight: '600',
    color: colors.textMuted,
    letterSpacing: 0.3,
    marginBottom: 6,
  },
  scanModeRow: {
    flexDirection: 'row',
    gap: 8,
    marginBottom: 8,
  },
  scanModeBtn: {
    flex: 1,
    borderWidth: 1.5,
    borderColor: colors.cardBorder,
    borderRadius: radii.button,
    paddingVertical: 10,
    alignItems: 'center',
  },
  scanModeBtnActive: {
    borderColor: colors.accentRed,
    backgroundColor: '#fdf6f4',
  },
  scanModeBtnText: {
    fontFamily: fonts.mono,
    fontSize: 12,
    fontWeight: '700',
    color: colors.textMuted,
    letterSpacing: 0.5,
  },
  scanModeBtnTextActive: {
    color: colors.accentRed,
  },
  scanConfigHint: {
    fontSize: 11,
    color: colors.textMuted,
    marginBottom: 4,
  },
  scanConfigInput: {
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: radii.input,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontSize: 11,
    fontFamily: fonts.mono,
    color: colors.textPrimary,
    backgroundColor: colors.inputBg,
  },
  scanConfigWarn: {
    fontFamily: fonts.mono,
    fontSize: 10,
    color: colors.copper,
    marginTop: 8,
  },
  scanConfigDone: {
    marginTop: 20,
    backgroundColor: colors.accentRed,
    borderRadius: radii.button,
    paddingVertical: 12,
    alignItems: 'center',
  },
  scanConfigDoneText: {
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '700',
    color: colors.cream,
    letterSpacing: 0.5,
  },
  // Info/Confirm modals
  infoModalCard: {
    backgroundColor: colors.background,
    borderRadius: radii.card,
    padding: 20,
    width: '90%',
    maxWidth: 340,
    borderWidth: 1,
    borderColor: colors.cardBorder,
  },
  infoModalTitle: {
    fontFamily: fonts.mono,
    fontSize: 16,
    fontWeight: '700',
    color: colors.textPrimary,
    marginBottom: 8,
  },
  infoModalMessage: {
    fontFamily: fonts.mono,
    fontSize: 13,
    color: colors.textMuted,
    lineHeight: 20,
  },
  infoModalButton: {
    backgroundColor: colors.accentRed,
    borderRadius: radii.button,
    paddingVertical: 12,
    alignItems: 'center',
    marginTop: 16,
  },
  infoModalButtonText: {
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '700',
    color: colors.cream,
    letterSpacing: 0.5,
  },
});
