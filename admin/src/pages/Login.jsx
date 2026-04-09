import { useState } from 'react';
import { useNavigate, Navigate } from 'react-router-dom';
import { useAuth } from '../auth.jsx';

export default function Login() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  if (user) return <Navigate to="/" replace />;

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const userData = await login(username, password);
      if (userData && userData.role !== 'ADMIN') {
        // Non-admin users cannot access the admin panel
        localStorage.removeItem('sentry_token');
        localStorage.removeItem('sentry_user');
        setError('Not authorized, contact admin');
        setLoading(false);
        return;
      }
      navigate('/');
    } catch (err) {
      setError(err.message === 'Not authorized' ? 'Not authorized, contact admin' : 'Wrong Username/Password');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-logo">
          <svg width="24" height="24" viewBox="0 0 32 32">
            <rect x="1" y="1" width="30" height="30" rx="5" fill="#8e2715"/>
            <rect x="7" y="6" width="7.5" height="20" rx="1.5" fill="none" stroke="#FCF4E3" strokeWidth="1.6"/>
            <rect x="17.5" y="6" width="7.5" height="20" rx="1.5" fill="none" stroke="#FCF4E3" strokeWidth="1.6"/>
            <line x1="8.5" y1="12" x2="13" y2="12" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
            <line x1="8.5" y1="16" x2="13" y2="16" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
            <line x1="8.5" y1="20" x2="13" y2="20" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
            <line x1="19" y1="12" x2="23.5" y2="12" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
            <line x1="19" y1="16" x2="23.5" y2="16" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
            <line x1="19" y1="20" x2="23.5" y2="20" stroke="#FCF4E3" strokeWidth="1" opacity="0.4"/>
          </svg>
          Sentry WMS
        </div>
        {error && <div className="login-error">{error}</div>}
        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>Username</label>
            <input
              className="form-input"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
            />
          </div>
          <div className="form-group">
            <label>Password</label>
            <input
              className="form-input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <button className="btn btn-primary" type="submit" disabled={loading}>
            {loading ? 'Signing in...' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
