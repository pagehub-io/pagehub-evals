/**
 * AppHeader — title row with hamburger / brand / breadcrumbs.
 * Mirrors app-prayers shape so the layout is visually familiar.
 */

import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { DrawerActions, useNavigation } from '@react-navigation/native';
import FontAwesome from '@expo/vector-icons/FontAwesome';

import { useTheme } from '@/contexts/ThemeContext';
import { Breadcrumbs } from '@/components/navigation/Breadcrumbs';
import { useIsMobile } from '@/hooks/useResponsive';

export function AppHeader() {
  const { theme } = useTheme();
  const navigation = useNavigation();
  const isMobile = useIsMobile();

  const styles = createStyles(theme);

  return (
    <View style={styles.container}>
      <View style={styles.row}>
        {isMobile && (
          <TouchableOpacity
            onPress={() => navigation.dispatch(DrawerActions.openDrawer())}
            style={styles.iconButton}
            accessibilityLabel="Open menu"
          >
            <FontAwesome name="bars" size={18} color={theme.colors.text} />
          </TouchableOpacity>
        )}
        <Text style={styles.brand}>Pagehub Evals</Text>
      </View>
      <Breadcrumbs />
    </View>
  );
}

function createStyles(theme: ReturnType<typeof useTheme>['theme']) {
  return StyleSheet.create({
    container: {
      backgroundColor: theme.colors.surface,
      borderBottomColor: theme.colors.border,
      borderBottomWidth: 1,
    },
    row: {
      flexDirection: 'row',
      alignItems: 'center',
      gap: 12,
      paddingHorizontal: 16,
      paddingTop: 12,
      paddingBottom: 4,
    },
    iconButton: {
      padding: 6,
    },
    brand: {
      fontSize: 18,
      fontWeight: '700',
      color: theme.colors.text,
    },
  });
}
