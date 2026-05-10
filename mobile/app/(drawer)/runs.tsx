import React from 'react';
import { ScrollView, Text, View, StyleSheet } from 'react-native';

import { useTheme } from '@/contexts/ThemeContext';

export default function RunsScreen() {
  const { theme } = useTheme();
  const styles = createStyles(theme);

  return (
    <ScrollView style={styles.container}>
      <View style={styles.empty}>
        <Text style={styles.title}>Runs</Text>
        <Text style={styles.body}>
          The verdict feed lives here. Each row will show: harness id, the agent&apos;s claim
          ("I fixed X by doing Y"), the engine&apos;s independent verdict, and links to the
          evidence (request/response pairs, twin-zero-traffic counts, timing).
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
