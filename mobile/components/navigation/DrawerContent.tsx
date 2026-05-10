/**
 * Custom Drawer Content
 *
 * Brand header → public nav (Collections, Runs, Environments, Requests)
 * → separator → settings/profile. Mirrors app-prayers layout pattern.
 */

import React from 'react';
import { View, Text, TouchableOpacity, StyleSheet, ScrollView } from 'react-native';
import { DrawerContentComponentProps } from '@react-navigation/drawer';
import { useRouter } from 'expo-router';
import FontAwesome from '@expo/vector-icons/FontAwesome';

import { useTheme } from '@/contexts/ThemeContext';
import { useAuth } from '@/contexts/AuthContext';

interface NavItem {
  id: string;
  icon: React.ComponentProps<typeof FontAwesome>['name'];
  label: string;
  route: string;
  requiresAuth?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { id: 'collections', icon: 'folder-open', label: 'Collections', route: '/(drawer)/' },
  { id: 'runs', icon: 'check-square-o', label: 'Runs', route: '/(drawer)/runs' },
  { id: 'environments', icon: 'sliders', label: 'Environments', route: '/(drawer)/environments' },
  { id: 'requests', icon: 'exchange', label: 'Requests', route: '/(drawer)/requests' },
  { id: 'settings', icon: 'cog', label: 'Settings', route: '/(drawer)/settings', requiresAuth: true },
];

export function DrawerContent(_props: DrawerContentComponentProps) {
  const router = useRouter();
  const { theme } = useTheme();
  const { isAuthenticated, user } = useAuth();

  const styles = createStyles(theme);

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.brand}>Pagehub Evals</Text>
        <Text style={styles.subtitle}>Ground-truth gate</Text>
      </View>

      <ScrollView style={styles.nav}>
        {NAV_ITEMS.map((item) => {
          const disabled = item.requiresAuth && !isAuthenticated;
          return (
            <TouchableOpacity
              key={item.id}
              disabled={disabled}
              onPress={() => router.push(item.route as never)}
              style={[styles.navItem, disabled && styles.navItemDisabled]}
            >
              <FontAwesome
                name={item.icon}
                size={16}
                color={disabled ? theme.colors.textMuted : theme.colors.text}
                style={styles.navIcon}
              />
              <Text style={[styles.navLabel, disabled && styles.navLabelDisabled]}>{item.label}</Text>
            </TouchableOpacity>
          );
        })}
      </ScrollView>

      <View style={styles.footer}>
        {isAuthenticated && user ? (
          <Text style={styles.footerText}>{user.email}</Text>
        ) : (
          <TouchableOpacity onPress={() => router.push('/(auth)/sign-in')}>
            <Text style={styles.footerLink}>Sign in</Text>
          </TouchableOpacity>
        )}
      </View>
    </View>
  );
}

function createStyles(theme: ReturnType<typeof useTheme>['theme']) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.colors.surface },
    header: {
      paddingHorizontal: 20,
      paddingTop: 24,
      paddingBottom: 16,
      borderBottomWidth: 1,
      borderBottomColor: theme.colors.border,
    },
    brand: { fontSize: 20, fontWeight: '700', color: theme.colors.text },
    subtitle: { fontSize: 12, color: theme.colors.textMuted, marginTop: 2 },
    nav: { flex: 1, paddingTop: 8 },
    navItem: {
      flexDirection: 'row',
      alignItems: 'center',
      paddingHorizontal: 20,
      paddingVertical: 12,
    },
    navItemDisabled: { opacity: 0.5 },
    navIcon: { marginRight: 12 },
    navLabel: { color: theme.colors.text, fontSize: 15 },
    navLabelDisabled: { color: theme.colors.textMuted },
    footer: {
      padding: 16,
      borderTopWidth: 1,
      borderTopColor: theme.colors.border,
    },
    footerText: { color: theme.colors.textMuted, fontSize: 13 },
    footerLink: { color: theme.colors.primary, fontSize: 14, fontWeight: '600' },
  });
}
