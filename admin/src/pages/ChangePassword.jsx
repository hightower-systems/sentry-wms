import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api.js';
import { useAuth } from '../auth.jsx';
import { friendlyError } from '../utils/friendlyError.js';

export default function ChangePassword() {
  const { user, refreshUser } = useAuth();
  const navigate = useNavigate();
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const forced = !!user?.must_change_password;

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');

    if (newPassword !== confirmPassword) {
      setError('New passwords do not match.');
      return;
    }

    setSubmitting(true);
    const res = await api.post('/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword,
    });

    if (!res || !res.ok) {
      const data = res ? await res.json().catch(() => ({})) : {};
      setError(friendlyError(data, 'Could not change password. Please try again.'));
      setSubmitting(false);
      return;
    }

    // Re-fetch /auth/me so the must_change_password flag in our context
    // picks up its new FALSE value. The router guard will then stop
    // redirecting here.
    await refreshUser();
    navigate('/', { replace: true });
  }

  return (
    <div style={{ padding: 24, maxWidth: 480 }}>
      {forced && (
        <div
          role="alert"
          className="forced-change-banner"
          style={{
            background: '#8e2716',
            color: '#fdf4e3',
            padding: '14px 18px',
            borderRadius: 6,
            marginBottom: 20,
            fontSize: 14,
            lineHeight: 1.4,
          }}
        >
          <strong>First-time setup:</strong> please choose a new admin password before continuing.
        </div>
      )}

      <h2 style={{ marginTop: 0, marginBottom: 20 }}>Change Password</h2>

      {error && <div className="login-error" style={{ marginBottom: 16 }}>{error}</div>}

      <form onSubmit={handleSubmit}>
        <div className="form-group">
          <label>Current password</label>
          <input
            className="form-input"
            type="password"
            autoComplete="current-password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            required
            autoFocus
          />
        </div>

        <div className="form-group">
          <label>New password</label>
          <input
            className="form-input"
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
          />
          <div style={{ fontSize: 12, color: 'rgba(0,0,0,0.55)', marginTop: 6 }}>
            At least 8 characters, one letter and one digit. Cannot be "admin".
          </div>
        </div>

        <div className="form-group">
          <label>Confirm new password</label>
          <input
            className="form-input"
            type="password"
            autoComplete="new-password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
          />
        </div>

        <div style={{ display: 'flex', gap: 10, marginTop: 20 }}>
          <button type="submit" className="btn btn-primary" disabled={submitting}>
            {submitting ? 'Saving...' : 'Change password'}
          </button>
          {!forced && (
            <button
              type="button"
              className="btn"
              onClick={() => navigate(-1)}
              disabled={submitting}
            >
              Cancel
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
