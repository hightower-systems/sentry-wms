import React, { useState, useEffect } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, KeyboardAvoidingView, Platform, Modal, Pressable } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { useAuth } from '../auth/AuthContext';
import WarehouseSelector from '../components/WarehouseSelector';
import client, { getStoredApiUrl, setApiUrl } from '../api/client';
import { colors, fonts, radii } from '../theme/styles';

const SENTRY_LOGIN_RENDERED = '__sentry_login_rendered__';

export default function LoginScreen() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [warehouses, setWarehouses] = useState([]);
  const [selectedWarehouse, setSelectedWarehouse] = useState(null);
  const [showWarehousePicker, setShowWarehousePicker] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [showServerModal, setShowServerModal] = useState(false);
  const [serverUrl, setServerUrlLocal] = useState('');
  const [serverDisplay, setServerDisplay] = useState('');
  const [renderGuard] = useState(() => {
    // Guard against duplicate renders  -  only allow one instance
    if (global[SENTRY_LOGIN_RENDERED]) return false;
    global[SENTRY_LOGIN_RENDERED] = true;
    return true;
  });

  useEffect(() => {
    return () => { global[SENTRY_LOGIN_RENDERED] = false; };
  }, []);

  useEffect(() => {
    // Restore cached username
    AsyncStorage.getItem('sentry_last_username').then((saved) => {
      if (saved) setUsername(saved);
    }).catch(() => {});

    // Load current server URL for display
    getStoredApiUrl().then((url) => setServerDisplay(url || ''));

    client.get('/api/warehouses/list')
      .then((resp) => {
        const list = resp.data.warehouses || [];
        setWarehouses(list);
        if (list.length === 1) setSelectedWarehouse(list[0].id);
      })
      .catch(() => setError('Could not load warehouses - check connection'));
  }, []);

  const selectedName = warehouses.find((w) => w.id === selectedWarehouse);

  const handleLogin = async () => {
    if (!username || !password) {
      setError('Username and password are required');
      return;
    }
    if (!selectedWarehouse) {
      setError('Select a warehouse');
      return;
    }
    setError('');
    setLoading(true);
    AsyncStorage.setItem('sentry_last_username', username).catch(() => {});
    try {
      await login(username, password, selectedWarehouse);
    } catch (err) {
      if (err.response?.status === 401) {
        setError('Invalid credentials');
        setPassword('');
      } else {
        setError('Connection error - check WiFi');
      }
    } finally {
      setLoading(false);
    }
  };

  const openServerModal = () => {
    getStoredApiUrl().then((url) => {
      setServerUrlLocal(url || '');
      setShowServerModal(true);
    });
  };

  const saveServerUrl = () => {
    const trimmed = serverUrl.trim();
    if (trimmed) {
      setApiUrl(trimmed);
      setServerDisplay(trimmed);
      // Reload warehouses with new server
      client.get('/api/warehouses/list')
        .then((resp) => {
          const list = resp.data.warehouses || [];
          setWarehouses(list);
          if (list.length === 1) setSelectedWarehouse(list[0].id);
          setError('');
        })
        .catch(() => setError('Could not connect to server'));
    }
    setShowServerModal(false);
  };

  if (!renderGuard) return null;

  return (
    <KeyboardAvoidingView
      style={styles.screen}
      behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
    >
      <View style={styles.container}>
        <View style={styles.logoSection}>
          <Text style={styles.logoText}>SENTRY</Text>
          <Text style={styles.logoSubtext}>WAREHOUSE MANAGEMENT</Text>
        </View>

        <View style={styles.form}>
          <TextInput
            style={styles.input}
            placeholder="Username"
            placeholderTextColor={colors.textPlaceholder}
            value={username}
            onChangeText={setUsername}
            autoCapitalize="none"
            autoCorrect={false}
          />
          <TextInput
            style={styles.input}
            placeholder="Password"
            placeholderTextColor={colors.textPlaceholder}
            value={password}
            onChangeText={setPassword}
            secureTextEntry
          />

          <TouchableOpacity
            style={styles.warehouseButton}
            onPress={() => setShowWarehousePicker(true)}
          >
            <Text style={styles.warehouseLabel}>WAREHOUSE</Text>
            <Text style={styles.warehouseValue}>
              {selectedName ? `${selectedName.code} - ${selectedName.name}` : 'Select warehouse...'}
            </Text>
          </TouchableOpacity>

          <TouchableOpacity
            style={[styles.loginButton, loading && styles.loginButtonDisabled]}
            onPress={handleLogin}
            disabled={loading}
          >
            <Text style={styles.loginButtonText}>{loading ? 'LOGGING IN...' : 'LOGIN'}</Text>
          </TouchableOpacity>

          {error ? <Text style={styles.error}>{error}</Text> : null}
        </View>
      </View>

      {/* Bottom: version + server URL (static, tap opens modal) */}
      <TouchableOpacity style={styles.bottomBar} onPress={openServerModal}>
        <Text style={styles.version}>v0.9.8</Text>
        {serverDisplay ? (
          <Text style={styles.serverUrlText} numberOfLines={1}>{serverDisplay}</Text>
        ) : null}
      </TouchableOpacity>

      {/* Server URL modal */}
      <Modal visible={showServerModal} transparent animationType="fade">
        <Pressable style={styles.modalOverlay} onPress={() => setShowServerModal(false)}>
          <Pressable style={styles.modalCard} onPress={() => {}}>
            <Text style={styles.modalTitle}>SERVER URL</Text>
            <TextInput
              style={styles.modalInput}
              value={serverUrl}
              onChangeText={setServerUrlLocal}
              placeholder="http://10.1.10.150:5000"
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              placeholderTextColor={colors.textPlaceholder}
              returnKeyType="done"
              onSubmitEditing={saveServerUrl}
              autoFocus
            />
            <View style={styles.modalActions}>
              <TouchableOpacity style={styles.modalSaveBtn} onPress={saveServerUrl}>
                <Text style={styles.modalSaveBtnText}>SAVE</Text>
              </TouchableOpacity>
              <TouchableOpacity style={styles.modalCancelBtn} onPress={() => setShowServerModal(false)}>
                <Text style={styles.modalCancelBtnText}>CANCEL</Text>
              </TouchableOpacity>
            </View>
          </Pressable>
        </Pressable>
      </Modal>

      <WarehouseSelector
        visible={showWarehousePicker}
        warehouses={warehouses}
        selected={selectedWarehouse}
        onSelect={(id) => {
          setSelectedWarehouse(id);
          setShowWarehousePicker(false);
        }}
      />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.background,
  },
  container: {
    flex: 1,
    justifyContent: 'center',
    paddingHorizontal: 32,
  },
  logoSection: {
    alignItems: 'center',
    marginBottom: 48,
  },
  logoText: {
    fontFamily: fonts.mono,
    fontSize: 36,
    fontWeight: '700',
    color: colors.accentRed,
    letterSpacing: 4,
  },
  logoSubtext: {
    fontFamily: fonts.mono,
    fontSize: 11,
    color: colors.textMuted,
    letterSpacing: 2,
    marginTop: 4,
  },
  form: {
    gap: 12,
  },
  input: {
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: radii.input,
    paddingHorizontal: 14,
    paddingVertical: 12,
    fontSize: 15,
    color: colors.textPrimary,
    backgroundColor: colors.inputBg,
    minHeight: 48,
  },
  warehouseButton: {
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: radii.input,
    paddingHorizontal: 14,
    paddingVertical: 12,
    minHeight: 48,
    justifyContent: 'center',
    backgroundColor: colors.inputBg,
  },
  warehouseLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    fontWeight: '600',
    color: colors.textMuted,
    letterSpacing: 0.5,
    marginBottom: 2,
  },
  warehouseValue: {
    fontFamily: fonts.mono,
    fontSize: 13,
    color: colors.textPrimary,
  },
  loginButton: {
    backgroundColor: colors.accentRed,
    borderRadius: radii.button,
    paddingVertical: 14,
    alignItems: 'center',
    minHeight: 48,
    marginTop: 8,
  },
  loginButtonDisabled: {
    opacity: 0.6,
  },
  loginButtonText: {
    color: colors.cream,
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    letterSpacing: 1,
  },
  error: {
    color: colors.accentRed,
    fontSize: 13,
    textAlign: 'center',
    marginTop: 8,
  },
  // Bottom bar (static, replaces inline server input)
  bottomBar: {
    paddingVertical: 12,
    paddingHorizontal: 32,
    alignItems: 'center',
  },
  version: {
    fontFamily: fonts.mono,
    fontSize: 11,
    color: colors.textPlaceholder,
    textAlign: 'center',
  },
  serverUrlText: {
    fontFamily: fonts.mono,
    fontSize: 9,
    color: colors.textPlaceholder,
    marginTop: 2,
    textAlign: 'center',
  },
  // Server URL modal
  modalOverlay: {
    flex: 1,
    backgroundColor: colors.overlay,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 24,
  },
  modalCard: {
    backgroundColor: colors.background,
    borderRadius: radii.card,
    padding: 20,
    width: '100%',
    maxWidth: 320,
    borderWidth: 1,
    borderColor: colors.cardBorder,
  },
  modalTitle: {
    fontFamily: fonts.mono,
    fontSize: 12,
    fontWeight: '700',
    color: colors.textMuted,
    letterSpacing: 0.5,
    marginBottom: 12,
  },
  modalInput: {
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: radii.input,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 13,
    fontFamily: fonts.mono,
    color: colors.textPrimary,
    backgroundColor: colors.inputBg,
    marginBottom: 16,
  },
  modalActions: {
    flexDirection: 'row',
    gap: 8,
  },
  modalSaveBtn: {
    flex: 1,
    backgroundColor: colors.accentRed,
    borderRadius: radii.button,
    paddingVertical: 12,
    alignItems: 'center',
  },
  modalSaveBtnText: {
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '700',
    color: colors.cream,
    letterSpacing: 0.5,
  },
  modalCancelBtn: {
    flex: 1,
    backgroundColor: colors.cardBorder,
    borderRadius: radii.button,
    paddingVertical: 12,
    alignItems: 'center',
  },
  modalCancelBtnText: {
    fontFamily: fonts.mono,
    fontSize: 13,
    fontWeight: '700',
    color: colors.textPrimary,
    letterSpacing: 0.5,
  },
});
