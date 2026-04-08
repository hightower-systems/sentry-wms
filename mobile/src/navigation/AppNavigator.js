import React from 'react';
import { ActivityIndicator, View } from 'react-native';
import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { useAuth } from '../auth/AuthContext';
import { colors } from '../theme/styles';

import LoginScreen from '../screens/LoginScreen';
import HomeScreen from '../screens/HomeScreen';
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

  if (isLoading) {
    return (
      <View style={{ flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: colors.background }}>
        <ActivityIndicator size="large" color={colors.accentRed} />
      </View>
    );
  }

  return (
    <NavigationContainer>
      <Stack.Navigator screenOptions={{ headerShown: false }}>
        {user ? (
          <>
            <Stack.Screen name="Home" component={HomeScreen} />
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
        ) : (
          <Stack.Screen name="Login" component={LoginScreen} />
        )}
      </Stack.Navigator>
    </NavigationContainer>
  );
}
