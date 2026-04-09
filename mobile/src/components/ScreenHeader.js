import React from 'react';
import { View, Text, TouchableOpacity } from 'react-native';
import { screenStyles } from '../theme/styles';

/**
 * Standard screen header with back button, title, and optional right element.
 *
 * Props:
 *  - title: string
 *  - onBack: () => void
 *  - right: ReactNode (optional – badge, menu button, etc.)
 */
export default function ScreenHeader({ title, onBack, right }) {
  return (
    <View style={screenStyles.header}>
      <TouchableOpacity style={screenStyles.backBtn} onPress={onBack}>
        <Text style={screenStyles.backText}>{'<'}</Text>
      </TouchableOpacity>
      <Text style={screenStyles.headerTitle}>{title}</Text>
      {right || <View style={{ width: 32 }} />}
    </View>
  );
}
