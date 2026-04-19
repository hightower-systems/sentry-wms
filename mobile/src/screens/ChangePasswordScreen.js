import React, { useEffect, useState } from 'react';
import {
  View,
  Text,
  TextInput,
  TouchableOpacity,
  StyleSheet,
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  BackHandler,
} from 'react-native';
import { useAuth } from '../auth/AuthContext';
import client from '../api/client';
import { colors, radii } from '../theme/styles';

export default function ChangePasswordScreen({ navigation }) {
  const { user, completePasswordChange, logout } = useAuth();
  const forced = !!user?.must_change_password;

  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // Disable swipe-back on iOS and the header back button when the flag is
  // set. The mobile navigator does not render a header for this screen,
  // but gestureEnabled still controls the swipe-from-left gesture.
  useEffect(() => {
    if (!forced) return;
    navigation.setOptions({
      gestureEnabled: false,
      headerShown: false,
      headerBackVisible: false,
    });
  }, [forced, navigation]);

  // Block Android hardware back while the flag is set. Returning true from
  // the listener swallows the event so the OS default (go back) never runs.
  useEffect(() => {
    if (!forced) return undefined;
    const sub = BackHandler.addEventListener('hardwareBackPress', () => true);
    return () => sub.remove();
  }, [forced]);

  const handleSubmit = async () => {
    setError('');

    if (newPassword !== confirmPassword) {
      setError('New passwords do not match.');
      return;
    }

    setSubmitting(true);
    try {
      await client.post('/api/auth/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
      });
      // Backend cleared must_change_password + password_changed_at in a
      // single transaction. Mirror that in the local user dict so the
      // navigator swaps out of forced mode.
      await completePasswordChange();
      // Navigator will render Home automatically now that the flag is
      // false. No explicit navigate needed.
    } catch (err) {
      const resp = err?.response?.data;
      const msg =
        (resp && resp.error) ||
        'Could not change password. Please try again.';
      setError(msg);
      setSubmitting(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={styles.screen}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <ScrollView
        contentContainerStyle={styles.scroll}
        keyboardShouldPersistTaps="handled"
      >
        {forced && (
          <View style={styles.banner} accessibilityRole="alert">
            <Text style={styles.bannerText}>
              <Text style={styles.bannerBold}>First-time setup: </Text>
              please choose a new admin password before continuing.
            </Text>
          </View>
        )}

        <Text style={styles.title}>Change Password</Text>

        {error ? <Text style={styles.error}>{error}</Text> : null}

        <Text style={styles.label}>Current password</Text>
        <TextInput
          style={styles.input}
          value={currentPassword}
          onChangeText={setCurrentPassword}
          secureTextEntry
          autoCapitalize="none"
          autoCorrect={false}
          autoFocus
        />

        <Text style={styles.label}>New password</Text>
        <TextInput
          style={styles.input}
          value={newPassword}
          onChangeText={setNewPassword}
          secureTextEntry
          autoCapitalize="none"
          autoCorrect={false}
        />
        <Text style={styles.hint}>
          At least 8 characters, one letter and one digit. Cannot be "admin".
        </Text>

        <Text style={styles.label}>Confirm new password</Text>
        <TextInput
          style={styles.input}
          value={confirmPassword}
          onChangeText={setConfirmPassword}
          secureTextEntry
          autoCapitalize="none"
          autoCorrect={false}
        />

        <TouchableOpacity
          style={[styles.button, submitting && styles.buttonDisabled]}
          onPress={handleSubmit}
          disabled={submitting}
        >
          <Text style={styles.buttonText}>
            {submitting ? 'Saving...' : 'Change password'}
          </Text>
        </TouchableOpacity>

        {!forced && (
          <TouchableOpacity
            style={styles.secondaryButton}
            onPress={() => navigation.goBack()}
            disabled={submitting}
          >
            <Text style={styles.secondaryButtonText}>Cancel</Text>
          </TouchableOpacity>
        )}

        {forced && (
          <TouchableOpacity
            style={styles.secondaryButton}
            onPress={logout}
            disabled={submitting}
          >
            <Text style={styles.secondaryButtonText}>Log out</Text>
          </TouchableOpacity>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  scroll: { padding: 20, paddingTop: 60 },
  banner: {
    backgroundColor: colors.accentRed,
    padding: 14,
    borderRadius: radii.small,
    marginBottom: 20,
  },
  bannerText: { color: colors.cream, fontSize: 14, lineHeight: 20 },
  bannerBold: { fontWeight: '700' },
  title: {
    fontSize: 22,
    fontWeight: '700',
    color: colors.textPrimary,
    marginBottom: 16,
  },
  label: {
    fontSize: 13,
    fontWeight: '600',
    color: colors.textSecondary,
    marginTop: 12,
    marginBottom: 6,
  },
  input: {
    backgroundColor: colors.inputBg,
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: radii.input,
    padding: 12,
    fontSize: 16,
    color: colors.textPrimary,
  },
  hint: { fontSize: 12, color: colors.textMuted, marginTop: 6 },
  error: {
    color: colors.danger,
    fontSize: 14,
    marginBottom: 12,
  },
  button: {
    backgroundColor: colors.accentRed,
    padding: 14,
    borderRadius: radii.button,
    alignItems: 'center',
    marginTop: 24,
    minHeight: 48,
    justifyContent: 'center',
  },
  buttonDisabled: { opacity: 0.6 },
  buttonText: { color: colors.cream, fontSize: 16, fontWeight: '700' },
  secondaryButton: {
    padding: 14,
    borderRadius: radii.button,
    alignItems: 'center',
    marginTop: 10,
    borderWidth: 1,
    borderColor: colors.border,
    minHeight: 48,
    justifyContent: 'center',
  },
  secondaryButtonText: { color: colors.textPrimary, fontSize: 15, fontWeight: '600' },
});
