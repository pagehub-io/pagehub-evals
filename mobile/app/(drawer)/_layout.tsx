import React from 'react';
import { Drawer } from 'expo-router/drawer';
import { GestureHandlerRootView } from 'react-native-gesture-handler';

import { DrawerContent } from '@/components/navigation/DrawerContent';
import { AppHeader } from '@/components/navigation/AppHeader';
import { useShouldAutoOpenDrawer } from '@/hooks/useResponsive';

export default function DrawerLayout() {
  const autoOpen = useShouldAutoOpenDrawer();

  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <Drawer
        drawerContent={(props) => <DrawerContent {...props} />}
        screenOptions={{
          header: () => <AppHeader />,
          drawerType: autoOpen ? 'permanent' : 'front',
          drawerStyle: { width: 280 },
        }}
      >
        <Drawer.Screen name="index" options={{ title: 'Collections' }} />
        <Drawer.Screen name="runs" options={{ title: 'Runs' }} />
        <Drawer.Screen name="environments" options={{ title: 'Environments' }} />
        <Drawer.Screen name="requests" options={{ title: 'Requests' }} />
        <Drawer.Screen name="settings" options={{ title: 'Settings' }} />
      </Drawer>
    </GestureHandlerRootView>
  );
}
