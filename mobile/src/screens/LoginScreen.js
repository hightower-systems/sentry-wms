import React, { useState, useEffect } from 'react';
import { View, Text, TextInput, TouchableOpacity, StyleSheet, KeyboardAvoidingView, Platform } from 'react-native';
import { useAuth } from '../auth/AuthContext';
import WarehouseSelector from '../components/WarehouseSelector';
import client from '../api/client';
import { colors, fonts } from '../theme/styles';

export default function LoginScreen() {
  const { login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [warehouses, setWarehouses] = useState([]);
  const [selectedWarehouse, setSelectedWarehouse] = useState(null);
  const [showWarehousePicker, setShowWarehousePicker] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    client.get('/api/warehouses/list')
      .then((resp) => {
        const list = resp.data.warehouses || [];
        setWarehouses(list);
        if (list.length === 1) setSelectedWarehouse(list[0].id);
      })
      .catch(() => setError('Could not load warehouses — check connection'));
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
    try {
      await login(username, password, selectedWarehouse);
    } catch (err) {
      if (err.response?.status === 401) {
        setError('Invalid credentials');
      } else {
        setError('Connection error — check WiFi');
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
            placeholderTextColor={colors.textSecondary}
            value={username}
            onChangeText={setUsername}
            autoCapitalize="none"
            autoCorrect={false}
          />
          <TextInput
            style={styles.input}
            placeholder="Password"
            placeholderTextColor={colors.textSecondary}
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
              {selectedName ? `${selectedName.code} — ${selectedName.name}` : 'Select warehouse...'}
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

        <Text style={styles.version}>v0.9.0</Text>
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
    borderColor: colors.border,
    borderRadius: 8,
    paddingHorizontal: 14,
    paddingVertical: 12,
    fontSize: 15,
    color: colors.textPrimary,
    backgroundColor: colors.background,
    minHeight: 48,
  },
  warehouseButton: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 8,
    paddingHorizontal: 14,
    paddingVertical: 12,
    minHeight: 48,
    justifyContent: 'center',
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
    borderRadius: 8,
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
  version: {
    fontFamily: fonts.mono,
    fontSize: 11,
    color: colors.textMuted,
    textAlign: 'center',
    position: 'absolute',
    bottom: 32,
    alignSelf: 'center',
  },
});
