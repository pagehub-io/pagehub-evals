import React from 'react';
import { Text, View, StyleSheet } from 'react-native';

import { useTheme } from '@/contexts/ThemeContext';

export default function SignInScreen() {
  const { theme } = useTheme();
  const styles = createStyles(theme);

  return (
    <View style={styles.container}>
      <Text style={styles.title}>Sign in</Text>
      <Text style={styles.body}>
        Sign-in flow lands when pagehub-auth integration ships. The scaffold
        renders this screen so route guards work.
      </Text>
    </View>
  );
}

function createStyles(theme: ReturnType<typeof useTheme>['theme']) {
  return StyleSheet.create({
    container: { flex: 1, padding: 24, gap: 12, backgroundColor: theme.colors.background },
    title: { fontSize: 22, fontWeight: '700', color: theme.colors.text },
    body: { fontSize: 14, color: theme.colors.textMuted, lineHeight: 20 },
  });
}
