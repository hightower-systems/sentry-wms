import React, { useState, useEffect } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, KeyboardAvoidingView, Platform, Modal, Pressable } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { useAuth } from '../auth/AuthContext';
import client, { getStoredApiUrl, setApiUrl, hasStoredApiUrl } from '../api/client';
import { colors, fonts, radii } from '../theme/styles';

const SENTRY_LOGIN_RENDERED = '__sentry_login_rendered__';

export default function LoginScreen() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [showServerModal, setShowServerModal] = useState(false);
  const [serverUrl, setServerUrlLocal] = useState('');
  const [serverDisplay, setServerDisplay] = useState('');
  const [needsSetup, setNeedsSetup] = useState(null);
  const [connectUrl, setConnectUrl] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState('');
  const [modalError, setModalError] = useState('');
  const [modalChecking, setModalChecking] = useState(false);
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
    AsyncStorage.getItem('sentry_last_username').then((saved) => {
      if (saved) setUsername(saved);
    }).catch(() => {});

    hasStoredApiUrl().then((hasUrl) => {
      if (hasUrl) {
        setNeedsSetup(false);
        getStoredApiUrl().then((url) => setServerDisplay(url || ''));
      } else {
        setNeedsSetup(true);
      }
    });
  }, []);

  const handleConnect = async () => {
    const trimmed = connectUrl.replace(/\/+$/, '').trim();
    if (!trimmed) {
      setConnectError('Enter a server URL');
      return;
    }
    setConnecting(true);
    setConnectError('');
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      const resp = await fetch(`${trimmed}/api/health`, { signal: controller.signal });
      clearTimeout(timeout);
      if (!resp.ok) throw new Error('Bad response');
      await setApiUrl(trimmed);
      setServerDisplay(trimmed);
      setNeedsSetup(false);
    } catch (err) {
      setConnectError(`Could not connect: ${err.message}`);
    } finally {
      setConnecting(false);
    }
  };

  const handleLogin = async () => {
    if (!username || !password) {
      setError('Username and password are required');
      return;
    }
    setError('');
    setLoading(true);
    AsyncStorage.setItem('sentry_last_username', username).catch(() => {});
    try {
      await login(username, password);
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
      setModalError('');
      setModalChecking(false);
      setShowServerModal(true);
    });
  };

  const saveServerUrl = async () => {
    const trimmed = serverUrl.replace(/\/+$/, '').trim();
    if (!trimmed) return;
    setModalChecking(true);
    setModalError('');
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      const resp = await fetch(`${trimmed}/api/health`, { signal: controller.signal });
      clearTimeout(timeout);
      if (!resp.ok) throw new Error('Bad response');
      await setApiUrl(trimmed);
      setServerDisplay(trimmed);
      setShowServerModal(false);
    } catch {
      setModalError('Could not connect to server');
    } finally {
      setModalChecking(false);
    }
  };

  if (!renderGuard) return null;
  if (needsSetup === null) return null;

  if (needsSetup) {
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
            <Text style={styles.setupLabel}>SERVER URL</Text>
            <TextInput
              style={styles.input}
              value={connectUrl}
              onChangeText={(t) => { setConnectUrl(t); setConnectError(''); }}
              placeholder="http://192.168.1.100:5000"
              placeholderTextColor={colors.textPlaceholder}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              returnKeyType="done"
              onSubmitEditing={handleConnect}
              autoFocus
            />

            <TouchableOpacity
              style={[styles.loginButton, connecting && styles.loginButtonDisabled]}
              onPress={handleConnect}
              disabled={connecting}
            >
              <Text style={styles.loginButtonText}>
                {connecting ? 'CONNECTING...' : 'CONNECT'}
              </Text>
            </TouchableOpacity>

            {connectError ? <Text style={styles.error}>{connectError}</Text> : null}
          </View>
        </View>
      </KeyboardAvoidingView>
    );
  }

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
            style={[styles.loginButton, loading && styles.loginButtonDisabled]}
            onPress={handleLogin}
            disabled={loading}
          >
            <Text style={styles.loginButtonText}>{loading ? 'LOGGING IN...' : 'LOGIN'}</Text>
          </TouchableOpacity>

          {error ? <Text style={styles.error}>{error}</Text> : null}

          <TouchableOpacity style={styles.serverInfo} onPress={openServerModal}>
            <Text style={styles.version}>v1.0.0</Text>
            {serverDisplay ? (
              <Text style={styles.serverUrlText} numberOfLines={1}>{serverDisplay}</Text>
            ) : null}
          </TouchableOpacity>
        </View>
      </View>

      {/* Server URL modal */}
      <Modal visible={showServerModal} transparent animationType="fade">
        <Pressable style={styles.modalOverlay} onPress={() => setShowServerModal(false)}>
          <Pressable style={styles.modalCard} onPress={() => {}}>
            <Text style={styles.modalTitle}>SERVER URL</Text>
            <TextInput
              style={styles.modalInput}
              value={serverUrl}
              onChangeText={(t) => { setServerUrlLocal(t); setModalError(''); }}
              placeholder="http://10.1.10.150:5000"
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              placeholderTextColor={colors.textPlaceholder}
              returnKeyType="done"
              onSubmitEditing={saveServerUrl}
              autoFocus
            />
            {modalError ? <Text style={styles.modalErrorText}>{modalError}</Text> : null}
            <View style={styles.modalActions}>
              <TouchableOpacity
                style={[styles.modalSaveBtn, modalChecking && styles.loginButtonDisabled]}
                onPress={saveServerUrl}
                disabled={modalChecking}
              >
                <Text style={styles.modalSaveBtnText}>{modalChecking ? 'CHECKING...' : 'SAVE'}</Text>
              </TouchableOpacity>
              <TouchableOpacity style={styles.modalCancelBtn} onPress={() => setShowServerModal(false)}>
                <Text style={styles.modalCancelBtnText}>CANCEL</Text>
              </TouchableOpacity>
            </View>
          </Pressable>
        </Pressable>
      </Modal>

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
  setupLabel: {
    fontFamily: fonts.mono,
    fontSize: 11,
    fontWeight: '600',
    color: colors.textMuted,
    letterSpacing: 0.5,
    marginBottom: 4,
  },
  serverInfo: {
    alignItems: 'center',
    marginTop: 16,
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
  modalErrorText: {
    color: colors.accentRed,
    fontSize: 12,
    marginBottom: 12,
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
