import React from 'react';
import { Link } from 'expo-router';
import { Text, View, StyleSheet } from 'react-native';

import { useTheme } from '@/contexts/ThemeContext';

export default function NotFound() {
  const { theme } = useTheme();
  const styles = createStyles(theme);
  return (
    <View style={styles.container}>
      <Text style={styles.title}>Not found</Text>
      <Link href="/(drawer)/" style={styles.link}>Go home</Link>
    </View>
  );
}

function createStyles(theme: ReturnType<typeof useTheme>['theme']) {
  return StyleSheet.create({
    container: { flex: 1, padding: 24, gap: 12, backgroundColor: theme.colors.background },
    title: { fontSize: 22, fontWeight: '700', color: theme.colors.text },
    link: { color: theme.colors.primary, fontSize: 14 },
  });
}
