import React, { useEffect } from 'react';
import { ActivityIndicator, View } from 'react-native';
import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import * as SplashScreen from 'expo-splash-screen';
import { useAuth } from '../auth/AuthContext';
import { colors } from '../theme/styles';

// Keep splash visible while auth state loads
SplashScreen.preventAutoHideAsync().catch(() => {});

import LoginScreen from '../screens/LoginScreen';
import HomeScreen from '../screens/HomeScreen';
import ChangePasswordScreen from '../screens/ChangePasswordScreen';
import ReceiveScreen from '../screens/ReceiveScreen';
import PutAwayScreen from '../screens/PutAwayScreen';
import PickScanScreen from '../screens/PickScanScreen';
import PickWalkScreen from '../screens/PickWalkScreen';
import PickCompleteScreen from '../screens/PickCompleteScreen';
import PackShipScreen from '../screens/PackShipScreen';
import PackScreen from '../screens/PackScreen';
import ShipScreen from '../screens/ShipScreen';
import CountScreen from '../screens/CountScreen';
import TransferScreen from '../screens/TransferScreen';

const Stack = createNativeStackNavigator();

export default function AppNavigator() {
  const { user, isLoading } = useAuth();

  useEffect(() => {
    if (!isLoading) {
      SplashScreen.hideAsync().catch(() => {});
    }
  }, [isLoading]);

  if (isLoading) {
    return null; // Splash screen stays visible
  }

  // Forced password change: when the user dict rehydrated from SecureStore
  // (or returned from login) carries must_change_password=true, route only
  // the change-password screen and nothing else. The force-kill + reopen
  // bypass is blocked because the flag survives in SecureStore until the
  // server-side column flips to false and we mirror that via
  // completePasswordChange.
  const forced = !!user?.must_change_password;

  return (
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        {user ? (
          forced ? (
            <Stack.Screen
              name="ChangePassword"
              component={ChangePasswordScreen}
              options={{ gestureEnabled: false }}
            />
          ) : (
            <>
              <Stack.Screen name="Home" component={HomeScreen} />
              <Stack.Screen name="ChangePassword" component={ChangePasswordScreen} />
              <Stack.Screen name="Receive" component={ReceiveScreen} />
              <Stack.Screen name="PutAway" component={PutAwayScreen} />
              <Stack.Screen name="PickScan" component={PickScanScreen} />
              <Stack.Screen name="PickWalk" component={PickWalkScreen} />
              <Stack.Screen name="PickComplete" component={PickCompleteScreen} />
              <Stack.Screen name="PackShip" component={PackShipScreen} />
              <Stack.Screen name="Pack" component={PackScreen} />
              <Stack.Screen name="Ship" component={ShipScreen} />
              <Stack.Screen name="Count" component={CountScreen} />
              <Stack.Screen name="Transfer" component={TransferScreen} />
            </>
          )
        ) : (
          <Stack.Screen name="Login" component={LoginScreen} />
        )}
      </Stack.Navigator>
    </NavigationContainer>
  );
}
