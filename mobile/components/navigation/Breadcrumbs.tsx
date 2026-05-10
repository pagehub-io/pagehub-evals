/**
 * Breadcrumbs — fleet-pattern. Drives by router segments.
 *
 * Mobile: single back-link plus current title.
 * Medium+: full crumb chain.
 */

import React from 'react';
import { Text, TouchableOpacity, View, StyleSheet } from 'react-native';
import { useRouter, useSegments } from 'expo-router';
import FontAwesome from '@expo/vector-icons/FontAwesome';

import { useTheme } from '@/contexts/ThemeContext';
import { useIsMobile } from '@/hooks/useResponsive';

const HUMAN: Record<string, string> = {
  '(drawer)': 'Home',
  '(auth)': 'Sign in',
  collections: 'Collections',
  runs: 'Runs',
  environments: 'Environments',
  requests: 'Requests',
  settings: 'Settings',
};

function humanize(seg: string): string {
  if (HUMAN[seg]) return HUMAN[seg];
  return seg.replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export function Breadcrumbs() {
  const segments = useSegments() as string[];
  const router = useRouter();
  const { theme } = useTheme();
  const isMobile = useIsMobile();

  // Skip route-group wrappers like "(drawer)" in the visible chain.
  const visible = segments.filter((s) => !(s.startsWith('(') && s.endsWith(')')));
  if (visible.length === 0) return null;

  const styles = createStyles(theme);

  if (isMobile && visible.length > 1) {
    const back = visible[visible.length - 2];
    return (
      <View style={styles.row}>
        <TouchableOpacity onPress={() => router.back()} style={styles.backButton}>
          <FontAwesome name="chevron-left" size={12} color={theme.colors.textMuted} />
          <Text style={styles.muted}>{humanize(back)}</Text>
        </TouchableOpacity>
      </View>
    );
  }

  return (
    <View style={styles.row}>
      {visible.map((seg, i) => {
        const isLast = i === visible.length - 1;
        return (
          <View key={`${seg}-${i}`} style={styles.crumb}>
            {i > 0 && (
              <Text style={styles.separator}> / </Text>
            )}
            <Text style={isLast ? styles.current : styles.muted}>{humanize(seg)}</Text>
          </View>
        );
      })}
    </View>
  );
}

function createStyles(theme: ReturnType<typeof useTheme>['theme']) {
  return StyleSheet.create({
    row: {
      flexDirection: 'row',
      alignItems: 'center',
      paddingHorizontal: 16,
      paddingVertical: 8,
      flexWrap: 'wrap',
    },
    backButton: {
      flexDirection: 'row',
      alignItems: 'center',
      gap: 6,
    },
    crumb: {
      flexDirection: 'row',
      alignItems: 'center',
    },
    separator: {
      color: theme.colors.textMuted,
      paddingHorizontal: 4,
    },
    current: {
      color: theme.colors.text,
      fontWeight: '600',
    },
    muted: {
      color: theme.colors.textMuted,
    },
  });
}
