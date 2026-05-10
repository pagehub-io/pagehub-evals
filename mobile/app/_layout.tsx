import 'react-native-reanimated';
import 'react-native-gesture-handler';
import React, { useEffect } from 'react';
import { Slot } from 'expo-router';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { SupportWidget } from '@pagehub-io/ux';

import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { tokenStorage } from '@/services/tokenStorage';

// Pagehub support backend URL. The widget POSTs /tickets etc. against
// pagehub (NOT this app's API). pagehub-auth has pagehub-evals registered
// so its verifier accepts our app_slug.
const PAGEHUB_API_BASE_URL =
  process.env.EXPO_PUBLIC_PAGEHUB_API_URL || 'http://localhost:8001';

async function getAuthToken(): Promise<string> {
  const token = await tokenStorage.getItem('access_token');
  return token ?? '';
}

function RootLayoutNav() {
  const { isAuthenticated, isLoading } = useAuth();
  const showSupportWidget = isAuthenticated && !isLoading;

  return (
    <>
      <Slot />
      {showSupportWidget && (
        <SupportWidget apiBaseUrl={PAGEHUB_API_BASE_URL} authToken={getAuthToken} />
      )}
    </>
  );
}

export default function RootLayout() {
  useEffect(() => {
    // Place for analytics/logger init when the JTBD pivot lands.
  }, []);

  return (
    <ErrorBoundary>
      <SafeAreaProvider>
        <AuthProvider>
          <ThemeProvider>
            <RootLayoutNav />
          </ThemeProvider>
        </AuthProvider>
      </SafeAreaProvider>
    </ErrorBoundary>
  );
}
