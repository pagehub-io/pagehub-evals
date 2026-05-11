import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';

import { useTheme } from '@/contexts/ThemeContext';
import {
  ApiError,
  runsApi,
  type RunEvaluationResult,
  type RunRequestResult,
  type RunResponse,
} from '@/services/api';

const POLL_INTERVAL_MS = 2000;
const MAX_POLL_DURATION_MS = 5 * 60 * 1000;
const MAX_CONSECUTIVE_POLL_FAILURES = 3;

type DetailState =
  | { kind: 'loading' }
  | { kind: 'ready'; run: RunResponse }
  | { kind: 'not-found' }
  | { kind: 'error'; message: string };

export default function RunDetailScreen() {
  const { theme } = useTheme();
  const styles = createStyles(theme);
  const router = useRouter();
  const params = useLocalSearchParams<{ id: string }>();
  const id = typeof params.id === 'string' ? params.id : '';

  const [state, setState] = useState<DetailState>({ kind: 'loading' });
  const [refreshing, setRefreshing] = useState(false);
  const [pollFailureCount, setPollFailureCount] = useState(0);
  const pollStartRef = useRef<number | null>(null);
  const [pollCapped, setPollCapped] = useState(false);

  // Refs the polling interval consults each tick, so we can keep the
  // interval registered for the lifetime of the screen (per-id) without
  // tearing down and rebuilding it on every successful tick (which is
  // what `[state, fetchOnce, pollCapped]` deps caused).
  const stateRef = useRef<DetailState>(state);
  const pollCappedRef = useRef<boolean>(pollCapped);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);
  useEffect(() => {
    pollCappedRef.current = pollCapped;
  }, [pollCapped]);

  const fetchOnce = useCallback(async (): Promise<RunResponse | null> => {
    try {
      return await runsApi.get(id);
    } catch (e) {
      if (e instanceof ApiError) {
        if (e.status === 403 || e.status === 404) {
          setState({ kind: 'not-found' });
          return null;
        }
        throw e;
      }
      throw e;
    }
  }, [id]);

  const loadInitial = useCallback(async () => {
    // Navigating to a different run id resets the poll budget — the
    // previous run's clock should not bleed into the next.
    pollStartRef.current = null;
    setPollCapped(false);
    setPollFailureCount(0);
    setState({ kind: 'loading' });
    try {
      const run = await fetchOnce();
      if (run) {
        setState({ kind: 'ready', run });
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Unknown error';
      setState({ kind: 'error', message });
    }
  }, [fetchOnce]);

  useEffect(() => {
    void loadInitial();
  }, [loadInitial]);

  // Stable interval — depends only on the run id + fetcher. We do NOT
  // depend on `state` or `pollCapped` (those would tear down the timer
  // on every tick). The tick consults refs.
  useEffect(() => {
    if (!id) return;
    const timer = setInterval(async () => {
      const snapshot = stateRef.current;
      if (snapshot.kind !== 'ready') return;
      const status = snapshot.run.status;
      if (status !== 'pending' && status !== 'running') return;
      if (pollCappedRef.current) return;
      if (pollStartRef.current === null) {
        pollStartRef.current = Date.now();
      }
      if (Date.now() - pollStartRef.current > MAX_POLL_DURATION_MS) {
        setPollCapped(true);
        return;
      }
      try {
        const run = await fetchOnce();
        if (run) {
          setState({ kind: 'ready', run });
          setPollFailureCount(0);
        }
      } catch {
        setPollFailureCount((n) => n + 1);
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [id, fetchOnce]);

  useEffect(() => {
    if (pollFailureCount >= MAX_CONSECUTIVE_POLL_FAILURES) {
      setPollCapped(true);
    }
  }, [pollFailureCount]);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const run = await fetchOnce();
      if (run) {
        setState({ kind: 'ready', run });
        setPollFailureCount(0);
      }
    } finally {
      setRefreshing(false);
    }
  }, [fetchOnce]);

  const resumePolling = useCallback(() => {
    pollStartRef.current = Date.now();
    setPollFailureCount(0);
    setPollCapped(false);
  }, []);

  if (state.kind === 'loading') {
    return (
      <View style={styles.center}>
        <ActivityIndicator />
      </View>
    );
  }

  if (state.kind === 'not-found') {
    return (
      <View style={styles.center}>
        <Text style={styles.errorTitle}>Run not found</Text>
        <Text style={styles.errorBody}>
          This run id doesn&apos;t exist or you don&apos;t have access.
        </Text>
        <TouchableOpacity onPress={() => router.push('/runs')} style={styles.retryButton}>
          <Text style={styles.retryButtonText}>Back to runs</Text>
        </TouchableOpacity>
      </View>
    );
  }

  if (state.kind === 'error') {
    return (
      <View style={styles.center}>
        <Text style={styles.errorTitle}>Couldn&apos;t load run</Text>
        <Text style={styles.errorBody}>{state.message}</Text>
        <TouchableOpacity onPress={loadInitial} style={styles.retryButton}>
          <Text style={styles.retryButtonText}>Retry</Text>
        </TouchableOpacity>
      </View>
    );
  }

  const run = state.run;
  const requests = run.evidence?.requests ?? [];
  const anyMissed = requests.some((r) => r.substitution_missed.length > 0);
  const engineError = run.evidence?.engine_error ?? null;
  const totalDuration = formatDuration(run.started_at, run.finished_at);

  return (
    <ScrollView
      style={styles.container}
      contentContainerStyle={styles.scrollContent}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
      stickyHeaderIndices={[0]}
    >
      <View style={styles.header}>
        <VerdictPillLarge state={run.status} />
        <Text style={styles.harnessId}>{run.harness_id ?? '—'}</Text>
        <Text style={styles.timing}>
          {run.started_at ? new Date(run.started_at).toLocaleString() : '—'} · {totalDuration}
        </Text>
        {(run.status === 'pending' || run.status === 'running') && !pollCapped && (
          <ActivityIndicator />
        )}
      </View>

      <View style={styles.claimBlock}>
        <Text style={styles.sectionHeader}>Harness claim</Text>
        <View style={styles.claimCard}>
          <Text style={styles.claimText}>
            {run.harness_claim ?? '(no claim recorded)'}
          </Text>
        </View>
      </View>

      {run.status === 'pending' && (
        <Text style={styles.muted}>Waiting for engine to pick up this run.</Text>
      )}
      {run.status === 'running' && (
        <Text style={styles.muted}>
          Engine is executing requests. This page refreshes every 2 seconds.
        </Text>
      )}

      {engineError && (
        <View style={styles.engineErrorCard}>
          <Text style={styles.engineErrorTitle}>Engine error</Text>
          <Text style={styles.engineErrorText}>{engineError}</Text>
        </View>
      )}

      {pollCapped && (run.status === 'pending' || run.status === 'running') && (
        <View style={styles.staleBlock}>
          <Text style={styles.muted}>still running…</Text>
          <TouchableOpacity onPress={resumePolling} style={styles.retryButton}>
            <Text style={styles.retryButtonText}>Retry</Text>
          </TouchableOpacity>
        </View>
      )}

      {anyMissed && (
        <View style={styles.warningBanner}>
          <Text style={styles.warningText}>
            unresolved:{' '}
            {Array.from(
              new Set(requests.flatMap((r) => r.substitution_missed)),
            ).join(', ')}
          </Text>
        </View>
      )}

      {requests.length === 0 ? (
        <Text style={styles.muted}>No requests executed.</Text>
      ) : (
        <View style={styles.requestsList}>
          {requests.map((req, idx) => (
            <RequestCard key={`${req.request_id}-${idx}`} req={req} />
          ))}
        </View>
      )}
    </ScrollView>
  );
}

function RequestCard({ req }: { req: RunRequestResult }) {
  const { theme } = useTheme();
  const styles = createStyles(theme);
  const glyph = req.transport_error
    ? '⚠'
    : req.passed
    ? '✓'
    : '✕';
  const statusColor = statusChipColor(theme, req.response_status);

  return (
    <View style={styles.requestCard}>
      <View style={styles.requestTopRow}>
        <Text style={styles.requestName}>{req.request_name}</Text>
        <Text style={styles.requestGlyph}>{glyph}</Text>
      </View>
      <Text style={styles.requestUrl}>
        {req.method} {req.url}
        {req.substitution_missed.length > 0 && (
          <Text style={{ color: theme.colors.warning }}> ⚠</Text>
        )}
      </Text>
      {req.transport_error ? (
        <Text style={styles.requestError}>{req.transport_error}</Text>
      ) : (
        <View style={styles.requestMetaRow}>
          <View style={[styles.statusChip, { backgroundColor: statusColor }]}>
            <Text style={styles.statusChipText}>
              {req.response_status === 0 ? 'ERR' : req.response_status}
            </Text>
          </View>
          <Text style={styles.latency}>{req.latency_ms}ms</Text>
        </View>
      )}
      {req.captured.length > 0 && (
        <Text style={styles.captured}>captured: {req.captured.join(', ')}</Text>
      )}
      {req.evaluations.length > 0 && (
        <View style={styles.evalList}>
          {req.evaluations.map((ev) => (
            <EvalRow key={ev.id} ev={ev} />
          ))}
        </View>
      )}
    </View>
  );
}

function EvalRow({ ev }: { ev: RunEvaluationResult }) {
  const { theme } = useTheme();
  const styles = createStyles(theme);
  const glyph = ev.passed ? '✓' : '✕';
  const detail = formatEvalDetail(ev);
  return (
    <View style={styles.evalRow}>
      <Text style={styles.evalGlyph}>{glyph}</Text>
      <View style={{ flex: 1 }}>
        <Text style={styles.evalName}>
          {ev.name} <Text style={styles.evalKind}>· {ev.kind}</Text>
        </Text>
        {ev.error ? (
          <Text style={[styles.evalDetail, { color: theme.colors.danger }]}>{ev.error}</Text>
        ) : (
          <Text style={styles.evalDetail}>{detail}</Text>
        )}
      </View>
    </View>
  );
}

function formatEvalDetail(ev: RunEvaluationResult): string {
  const d = ev.detail || {};
  switch (ev.kind) {
    case 'status_eq':
      return `expected ${String(d.expected)}, got ${String(d.observed)}`;
    case 'json_path_eq':
      if (d.missing) return `${String(d.path)}: missing`;
      return `${String(d.path)}: expected ${JSON.stringify(d.expected)}, got ${JSON.stringify(d.observed)}`;
    case 'header_present':
      return `${String(d.header)}: ${d.present ? 'present' : 'missing'}`;
    case 'body_contains':
      return `contains ${JSON.stringify(d.needle)}: ${String(d.present)}`;
    default:
      return JSON.stringify(d);
  }
}

function statusChipColor(
  theme: ReturnType<typeof useTheme>['theme'],
  status: number,
): string {
  if (status === 0) return theme.colors.danger;
  if (status >= 200 && status < 300) return theme.colors.success;
  if (status >= 300 && status < 400) return theme.colors.primary;
  if (status >= 400 && status < 500) return theme.colors.warning;
  if (status >= 500) return theme.colors.danger;
  return theme.colors.textMuted;
}

function VerdictPillLarge({ state }: { state: RunResponse['status'] }) {
  const { theme } = useTheme();
  const styles = createStyles(theme);
  const config = pillConfig(theme, state);
  return (
    <View
      style={[
        styles.pillLarge,
        {
          backgroundColor: config.bg,
          borderColor: config.border ?? config.bg,
          borderWidth: config.border ? 1 : 0,
        },
      ]}
    >
      <Text style={[styles.pillLargeText, { color: config.fg }]}>
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
    scrollContent: { padding: 16, gap: 16 },
    center: {
      flex: 1,
      alignItems: 'center',
      justifyContent: 'center',
      padding: 24,
      gap: 12,
      backgroundColor: theme.colors.background,
    },
    errorTitle: { fontSize: 18, fontWeight: '600', color: theme.colors.text },
    errorBody: { fontSize: 13, color: theme.colors.textMuted, textAlign: 'center' },
    retryButton: { paddingHorizontal: 16, paddingVertical: 8 },
    retryButtonText: { color: theme.colors.primary, fontWeight: '600' },
    header: {
      backgroundColor: theme.colors.background,
      gap: 6,
      paddingBottom: 8,
      borderBottomColor: theme.colors.border,
      borderBottomWidth: 1,
    },
    harnessId: { fontFamily: 'monospace', fontSize: 12, color: theme.colors.textMuted },
    timing: { fontSize: 12, color: theme.colors.textMuted },
    sectionHeader: { fontSize: 14, fontWeight: '600', color: theme.colors.text },
    claimBlock: { gap: 6 },
    claimCard: {
      backgroundColor: theme.colors.surface,
      borderColor: theme.colors.border,
      borderWidth: 1,
      borderRadius: 6,
      padding: 12,
    },
    claimText: { fontSize: 14, color: theme.colors.text, lineHeight: 20 },
    muted: { fontSize: 13, color: theme.colors.textMuted },
    engineErrorCard: {
      backgroundColor: theme.colors.surface,
      borderColor: theme.colors.warning,
      borderWidth: 1,
      borderRadius: 6,
      padding: 12,
      gap: 4,
    },
    engineErrorTitle: { fontSize: 14, fontWeight: '600', color: theme.colors.warning },
    engineErrorText: { fontSize: 13, color: theme.colors.text },
    staleBlock: { flexDirection: 'row', alignItems: 'center', gap: 8 },
    warningBanner: {
      backgroundColor: theme.colors.surface,
      borderColor: theme.colors.warning,
      borderWidth: 1,
      borderRadius: 6,
      padding: 10,
    },
    warningText: { fontSize: 13, color: theme.colors.warning },
    requestsList: { gap: 12 },
    requestCard: {
      backgroundColor: theme.colors.surface,
      borderColor: theme.colors.border,
      borderWidth: 1,
      borderRadius: 6,
      padding: 12,
      gap: 6,
    },
    requestTopRow: { flexDirection: 'row', justifyContent: 'space-between' },
    requestName: { fontSize: 14, fontWeight: '600', color: theme.colors.text, flex: 1 },
    requestGlyph: { fontSize: 16, fontWeight: '700', color: theme.colors.text },
    requestUrl: { fontFamily: 'monospace', fontSize: 12, color: theme.colors.textMuted },
    requestError: { fontSize: 13, color: theme.colors.danger },
    requestMetaRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
    statusChip: { paddingHorizontal: 6, paddingVertical: 2, borderRadius: 4 },
    statusChipText: { color: '#ffffff', fontSize: 11, fontWeight: '600' },
    latency: { fontSize: 12, color: theme.colors.textMuted },
    captured: { fontSize: 12, color: theme.colors.textMuted },
    evalList: { gap: 4, marginTop: 4, paddingLeft: 8 },
    evalRow: { flexDirection: 'row', alignItems: 'flex-start', gap: 6 },
    evalGlyph: { fontSize: 13, color: theme.colors.text, width: 14 },
    evalName: { fontSize: 13, color: theme.colors.text },
    evalKind: { color: theme.colors.textMuted, fontSize: 12 },
    evalDetail: { fontSize: 12, color: theme.colors.textMuted },
    pillLarge: {
      alignSelf: 'flex-start',
      paddingHorizontal: 12,
      paddingVertical: 4,
      borderRadius: 14,
    },
    pillLargeText: { fontSize: 14, fontWeight: '700' },
  });
}
