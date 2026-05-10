import React from 'react';
import { ScrollView, Text, View, StyleSheet } from 'react-native';

import { useTheme } from '@/contexts/ThemeContext';

export default function RequestsScreen() {
  const { theme } = useTheme();
  const styles = createStyles(theme);

  return (
    <ScrollView style={styles.container}>
      <View style={styles.empty}>
        <Text style={styles.title}>Requests</Text>
        <Text style={styles.body}>
          HTTP request templates. {'{{VAR}}'} substitution is resolved against the
          environment chosen at run-time.
        </Text>
      </View>
    </ScrollView>
  );
}

function createStyles(theme: ReturnType<typeof useTheme>['theme']) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.colors.background },
    empty: { padding: 24, gap: 12 },
    title: { fontSize: 22, fontWeight: '700', color: theme.colors.text },
    body: { fontSize: 14, color: theme.colors.textMuted, lineHeight: 20 },
  });
}
