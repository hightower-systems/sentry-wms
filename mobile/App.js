import React from 'react';
import { AuthProvider } from './src/auth/AuthContext';
import { ScanSettingsProvider } from './src/context/ScanSettingsContext';
import AppNavigator from './src/navigation/AppNavigator';

export default function App() {
  return (
    <AuthProvider>
      <ScanSettingsProvider>
        <AppNavigator />
      </ScanSettingsProvider>
    </AuthProvider>
  );
}
