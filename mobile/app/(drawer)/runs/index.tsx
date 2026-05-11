import React, { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { useRouter } from 'expo-router';

import { useTheme } from '@/contexts/ThemeContext';
import { ApiError, runsApi, type RunResponse } from '@/services/api';

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; runs: RunResponse[] }
  | { kind: 'error'; message: string; status?: number };

export default function RunsScreen() {
  const { theme } = useTheme();
  const router = useRouter();
  const styles = createStyles(theme);
  const [state, setState] = useState<LoadState>({ kind: 'loading' });
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await runsApi.list();
      setState({ kind: 'ready', runs: data.items });
    } catch (e) {
      if (e instanceof ApiError) {
        setState({ kind: 'error', message: e.message, status: e.status });
      } else if (e instanceof Error) {
        setState({ kind: 'error', message: e.message });
      } else {
        setState({ kind: 'error', message: 'Unknown error' });
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await load();
    } finally {
      setRefreshing(false);
    }
  }, [load]);

  if (state.kind === 'loading') {
    return (
      <View style={styles.center}>
        <ActivityIndicator />
      </View>
    );
  }

  if (state.kind === 'error') {
    const isAuth = state.status === 401 || state.status === 403;
    return (
      <View style={styles.center}>
        <Text style={styles.errorTitle}>
          {isAuth ? 'Not authorized to view runs' : "Couldn't load runs"}
        </Text>
        <Text style={styles.errorBody}>{state.message}</Text>
        {!isAuth && (
          <TouchableOpacity onPress={load} style={styles.retryButton}>
            <Text style={styles.retryButtonText}>Retry</Text>
          </TouchableOpacity>
        )}
      </View>
    );
  }

  if (state.runs.length === 0) {
    return (
      <ScrollView
        style={styles.container}
        contentContainerStyle={styles.emptyContainer}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
      >
        <Text style={styles.emptyTitle}>No runs yet</Text>
        <Text style={styles.emptyBody}>
          Runs appear here when a harness POSTs to /v1/runs with its claim.
        </Text>
      </ScrollView>
    );
  }

  return (
    <ScrollView
      style={styles.container}
      contentContainerStyle={styles.listContent}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
    >
      {state.runs.map((run) => (
        <RunRow key={run.id} run={run} onPress={() => router.push(`/runs/${run.id}`)} />
      ))}
    </ScrollView>
  );
}

function RunRow({ run, onPress }: { run: RunResponse; onPress: () => void }) {
  const { theme } = useTheme();
  const styles = createStyles(theme);
  const verdictText = run.status;
  const terminal = run.status === 'passed' || run.status === 'failed' || run.status === 'error';
  const requestCount = run.evidence?.requests?.length ?? 0;
  const duration = formatDuration(run.started_at, run.finished_at);

  return (
    <TouchableOpacity style={styles.card} onPress={onPress}>
      <View style={styles.cardTopRow}>
        <VerdictPill state={run.status} />
        <Text style={styles.harnessId}>{run.harness_id ?? '—'}</Text>
        <Text style={styles.relativeTime}>{relativeTime(run.created_at)}</Text>
      </View>
      <Text style={styles.claim} numberOfLines={2}>
        {run.harness_claim ?? '(no claim recorded)'}
      </Text>
      {terminal && (
        <Text style={styles.summary}>
          {requestCount} request{requestCount === 1 ? '' : 's'} · {duration}
        </Text>
      )}
    </TouchableOpacity>
  );
}

function VerdictPill({ state }: { state: RunResponse['status'] }) {
  const { theme } = useTheme();
  const styles = createStyles(theme);
  const config = pillConfig(theme, state);
  return (
    <View
      style={[
        styles.pill,
        {
          backgroundColor: config.bg,
          borderColor: config.border ?? config.bg,
          borderWidth: config.border ? 1 : 0,
        },
      ]}
    >
      <Text style={[styles.pillText, { color: config.fg }]}>
        {config.glyph} {state}
      </Text>
    </View>
  );
}

function pillConfig(
  theme: ReturnType<typeof useTheme>['theme'],
  state: RunResponse['status'],
): { bg: string; fg: string; glyph: string; border?: string } {
  switch (state) {
    case 'passed':
      return { bg: theme.colors.success, fg: '#ffffff', glyph: '✓' };
    case 'failed':
      return { bg: theme.colors.danger, fg: '#ffffff', glyph: '✕' };
    case 'error':
      return { bg: theme.colors.warning, fg: '#ffffff', glyph: '!' };
    case 'running':
      return { bg: theme.colors.primary, fg: '#ffffff', glyph: '•' };
    case 'pending':
    default:
      return {
        bg: theme.colors.surface,
        fg: theme.colors.textMuted,
        glyph: '·',
        border: theme.colors.border,
      };
  }
}

function relativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const delta = Math.max(0, Date.now() - t);
  const seconds = Math.floor(delta / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return 'yesterday';
  return `${days}d ago`;
}

function formatDuration(startIso: string | null, finishIso: string | null): string {
  if (!startIso || !finishIso) return '—';
  const ms = new Date(finishIso).getTime() - new Date(startIso).getTime();
  if (Number.isNaN(ms)) return '—';
  if (ms < 1) return '<1ms';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function createStyles(theme: ReturnType<typeof useTheme>['theme']) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.colors.background },
    listContent: { padding: 16, gap: 12 },
    emptyContainer: {
      flexGrow: 1,
      padding: 24,
      gap: 12,
      alignItems: 'center',
      justifyContent: 'center',
    },
    emptyTitle: { fontSize: 18, fontWeight: '600', color: theme.colors.text },
    emptyBody: {
      fontSize: 14,
      color: theme.colors.textMuted,
      textAlign: 'center',
      lineHeight: 20,
    },
    center: {
      flex: 1,
      alignItems: 'center',
      justifyContent: 'center',
      padding: 24,
      gap: 12,
      backgroundColor: theme.colors.background,
    },
    errorTitle: { fontSize: 16, fontWeight: '600', color: theme.colors.text },
    errorBody: { fontSize: 13, color: theme.colors.textMuted, textAlign: 'center' },
    retryButton: { paddingHorizontal: 16, paddingVertical: 8 },
    retryButtonText: { color: theme.colors.primary, fontWeight: '600' },
    card: {
      backgroundColor: theme.colors.surface,
      borderRadius: 8,
      padding: 12,
      gap: 6,
      borderWidth: 1,
      borderColor: theme.colors.border,
    },
    cardTopRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
    harnessId: {
      fontFamily: 'monospace',
      fontSize: 12,
      color: theme.colors.text,
      flex: 1,
    },
    relativeTime: { fontSize: 12, color: theme.colors.textMuted },
    claim: { fontSize: 14, color: theme.colors.text, lineHeight: 18 },
    summary: { fontSize: 12, color: theme.colors.textMuted },
    pill: {
      paddingHorizontal: 8,
      paddingVertical: 2,
      borderRadius: 12,
    },
    pillText: { fontSize: 11, fontWeight: '600' },
  });
}
