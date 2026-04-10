import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import { AppState } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import client, { setLogoutHandler, initApiUrl } from '../api/client';

const AuthContext = createContext(null);

const SESSION_TIMEOUT_MS = 8 * 60 * 60 * 1000;

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [warehouseId, setWarehouseId] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const appState = useRef(AppState.currentState);

  const logout = useCallback(async () => {
    await AsyncStorage.multiRemove(['jwt_token', 'user_data', 'warehouse_id', 'login_timestamp']);
    setUser(null);
    setWarehouseId(null);
  }, []);

  useEffect(() => {
    setLogoutHandler(logout);
  }, [logout]);

  const checkSession = useCallback(async () => {
    const timestamp = await AsyncStorage.getItem('login_timestamp');
    if (timestamp && Date.now() - parseInt(timestamp, 10) > SESSION_TIMEOUT_MS) {
      await logout();
      return false;
    }
    return true;
  }, [logout]);

  useEffect(() => {
    (async () => {
      try {
        // Load saved API URL from AsyncStorage BEFORE any API calls
        await initApiUrl();
        const valid = await checkSession();
        if (!valid) {
          setIsLoading(false);
          return;
        }
        const token = await AsyncStorage.getItem('jwt_token');
        const userData = await AsyncStorage.getItem('user_data');
        const wId = await AsyncStorage.getItem('warehouse_id');
        if (token && userData) {
          setUser(JSON.parse(userData));
          setWarehouseId(wId ? parseInt(wId, 10) : null);
        }
      } catch {
        await logout();
      } finally {
        setIsLoading(false);
      }
    })();
  }, []);

  useEffect(() => {
    const sub = AppState.addEventListener('change', (nextState) => {
      if (appState.current.match(/inactive|background/) && nextState === 'active') {
        checkSession();
      }
      appState.current = nextState;
    });
    return () => sub.remove();
  }, [checkSession]);

  const login = async (username, password, selectedWarehouseId) => {
    const resp = await client.post('/api/auth/login', { username, password });
    const { token, user: userData } = resp.data;
    await AsyncStorage.setItem('jwt_token', token);
    await AsyncStorage.setItem('user_data', JSON.stringify(userData));
    await AsyncStorage.setItem('warehouse_id', String(selectedWarehouseId));
    await AsyncStorage.setItem('login_timestamp', String(Date.now()));
    setUser(userData);
    setWarehouseId(selectedWarehouseId);
  };

  const switchWarehouse = async (newWarehouseId) => {
    await AsyncStorage.setItem('warehouse_id', String(newWarehouseId));
    setWarehouseId(newWarehouseId);
  };

  return (
    <AuthContext.Provider value={{ user, warehouseId, isLoading, login, logout, switchWarehouse }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
