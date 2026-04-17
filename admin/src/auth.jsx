import { createContext, useContext, useState, useEffect } from 'react';
import { api } from './api.js';
import { friendlyError } from './utils/friendlyError.js';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // V-045: session lives in an HttpOnly cookie; there is no token in
    // localStorage to read. Ask the API who we are. If the cookie is
    // missing or expired, /auth/me returns 401 and we stay unauthenticated.
    let cancelled = false;
    api.get('/auth/me').then(async (res) => {
      if (cancelled) return;
      if (res && res.ok) {
        const data = await res.json();
        if (data.role === 'ADMIN') {
          setUser(data);
        }
      }
      setLoading(false);
    }).catch(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, []);

  async function login(username, password) {
    const res = await api.post('/auth/login', { username, password });
    if (!res || !res.ok) {
      const data = res ? await res.json().catch(() => ({})) : {};
      // V-021: never echo the raw backend error string to the user.
      throw new Error(friendlyError(data, 'Login failed. Please try again.'));
    }
    const data = await res.json();
    if (data.user.role !== 'ADMIN') {
      // Not an admin -- ask the server to clear the cookies we just got.
      await api.post('/auth/logout', {});
      throw new Error('Not authorized');
    }
    setUser(data.user);
    return data.user;
  }

  async function logout() {
    try {
      await api.post('/auth/logout', {});
    } finally {
      setUser(null);
    }
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
