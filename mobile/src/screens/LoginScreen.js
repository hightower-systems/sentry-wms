import React, { useState, useEffect } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, KeyboardAvoidingView, Platform } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { useAuth } from '../auth/AuthContext';
import WarehouseSelector from '../components/WarehouseSelector';
import client, { getStoredApiUrl, setApiUrl } from '../api/client';
import { colors, fonts, radii } from '../theme/styles';

export default function LoginScreen() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [warehouses, setWarehouses] = useState([]);
  const [selectedWarehouse, setSelectedWarehouse] = useState(null);
  const [showWarehousePicker, setShowWarehousePicker] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [showServerConfig, setShowServerConfig] = useState(false);
  const [serverUrl, setServerUrlLocal] = useState('');

  useEffect(() => {
    // Restore cached username
    AsyncStorage.getItem('sentry_last_username').then((saved) => {
      if (saved) setUsername(saved);
    }).catch(() => {});

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
    // Cache username for next login attempt
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

        <TouchableOpacity
          style={styles.versionBtn}
          onPress={() => {
            getStoredApiUrl().then(setServerUrlLocal);
            setShowServerConfig(!showServerConfig);
          }}
        >
          <Text style={styles.version}>v0.9.7</Text>
        </TouchableOpacity>

        {showServerConfig && (
          <View style={styles.serverConfig}>
            <Text style={styles.serverLabel}>SERVER</Text>
            <TextInput
              style={styles.serverInput}
              value={serverUrl}
              onChangeText={setServerUrlLocal}
              onBlur={() => {
                if (serverUrl.trim()) {
                  setApiUrl(serverUrl.trim());
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
              }}
              placeholder="http://10.1.10.150:5000"
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              placeholderTextColor={colors.textPlaceholder}
              returnKeyType="done"
              onSubmitEditing={() => {
                if (serverUrl.trim()) {
                  setApiUrl(serverUrl.trim());
                  client.get('/api/warehouses/list')
                    .then((resp) => {
                      const list = resp.data.warehouses || [];
                      setWarehouses(list);
                      if (list.length === 1) setSelectedWarehouse(list[0].id);
                      setError('');
                    })
                    .catch(() => setError('Could not connect to server'));
                }
              }}
            />
          </View>
        )}
      </View>

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
  versionBtn: {
    position: 'absolute',
    bottom: 24,
    left: 0,
    right: 0,
    alignItems: 'center',
    padding: 8,
  },
  version: {
    fontFamily: fonts.mono,
    fontSize: 11,
    color: colors.textPlaceholder,
    textAlign: 'center',
  },
  serverConfig: {
    position: 'absolute',
    bottom: 56,
    left: 32,
    right: 32,
  },
  serverLabel: {
    fontFamily: fonts.mono,
    fontSize: 9,
    fontWeight: '600',
    color: colors.textMuted,
    letterSpacing: 0.5,
    marginBottom: 4,
  },
  serverInput: {
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: radii.input,
    paddingHorizontal: 12,
    paddingVertical: 8,
    fontSize: 12,
    fontFamily: fonts.mono,
    color: colors.textPrimary,
    backgroundColor: colors.inputBg,
  },
});
