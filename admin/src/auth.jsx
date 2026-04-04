import { createContext, useContext, useState, useEffect } from 'react';
import { api } from './api.js';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const token = localStorage.getItem('sentry_token');
    const saved = localStorage.getItem('sentry_user');
    if (token && saved) {
      setUser(JSON.parse(saved));
    }
    setLoading(false);
  }, []);

  async function login(username, password) {
    const res = await api.post('/auth/login', { username, password });
    if (!res || !res.ok) {
      const data = res ? await res.json() : {};
      throw new Error(data.error || 'Login failed');
    }
    const data = await res.json();
    localStorage.setItem('sentry_token', data.token);
    localStorage.setItem('sentry_user', JSON.stringify(data.user));
    setUser(data.user);
  }

  function logout() {
    localStorage.removeItem('sentry_token');
    localStorage.removeItem('sentry_user');
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
