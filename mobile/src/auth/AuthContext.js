import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import { AppState } from 'react-native';
import client, { setLogoutHandler, initApiUrl } from '../api/client';
import {
  clearAllAuth,
  getAuthItem,
  runAuthStorageMigration,
  setAuthItem,
} from './secureStorage';

const AuthContext = createContext(null);

const SESSION_TIMEOUT_MS = 8 * 60 * 60 * 1000;

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [warehouseId, setWarehouseId] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const appState = useRef(AppState.currentState);

  const logout = useCallback(async () => {
    await clearAllAuth();
    setUser(null);
    setWarehouseId(null);
  }, []);

  useEffect(() => {
    setLogoutHandler(logout);
  }, [logout]);

  const checkSession = useCallback(async () => {
    const timestamp = await getAuthItem('login_timestamp');
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
        // V-047: lift any plaintext auth keys left over from a prior install
        // out of AsyncStorage and into the keystore before reading them.
        await runAuthStorageMigration();
        const valid = await checkSession();
        if (!valid) {
          setIsLoading(false);
          return;
        }
        const token = await getAuthItem('jwt_token');
        const userData = await getAuthItem('user_data');
        const wId = await getAuthItem('warehouse_id');
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

  const login = async (username, password) => {
    const resp = await client.post('/api/auth/login', { username, password });
    const { token, user: userData } = resp.data;
    await setAuthItem('jwt_token', token);
    await setAuthItem('user_data', JSON.stringify(userData));
    await setAuthItem('login_timestamp', String(Date.now()));
    setUser(userData);
    // warehouseId stays null - HomeScreen will prompt for selection
  };

  const switchWarehouse = async (newWarehouseId) => {
    await setAuthItem('warehouse_id', String(newWarehouseId));
    setWarehouseId(newWarehouseId);
  };

  return (
    <AuthContext.Provider value={{ user, warehouseId, isLoading, login, logout, switchWarehouse }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
