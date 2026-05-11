/**
 * Minimal API client to the pagehub-evals backend. Operator surfaces
 * call this; the SupportWidget uses its own (pagehub) base URL.
 */

import { tokenStorage } from '@/services/tokenStorage';

const API_BASE_URL =
  process.env.EXPO_PUBLIC_API_URL || 'http://localhost:8002';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await tokenStorage.getItem('access_token');
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const r = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  if (!r.ok) {
    throw new ApiError(r.status, `${r.status} ${r.statusText}`);
  }
  return (await r.json()) as T;
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body: unknown) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body) }),
  patch: <T,>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  del: <T,>(path: string) => request<T>(path, { method: 'DELETE' }),
};

// ---------- run shapes (mirror api/runs/schemas.py) ----------

export type RunStatus = 'pending' | 'running' | 'passed' | 'failed' | 'error';
export type RunVerdict = 'passed' | 'failed' | 'error';

export type RunEvaluationResult = {
  id: string;
  name: string;
  kind: string;
  passed: boolean;
  detail: Record<string, unknown>;
  error: string | null;
};

export type RunRequestResult = {
  request_id: string;
  request_name: string;
  method: string;
  url: string;
  response_status: number;
  response_headers: Record<string, string>;
  response_body_excerpt: unknown;
  latency_ms: number;
  transport_error: string | null;
  substitution_missed: string[];
  captured: string[];
  evaluations: RunEvaluationResult[];
  passed: boolean;
};

export type RunEvidence = {
  requests: RunRequestResult[];
  engine_error?: string;
};

export type RunResponse = {
  id: string;
  created_by_kind: string;
  created_by_id: string;
  collection_id: string | null;
  environment_id: string | null;
  harness_id: string | null;
  harness_claim: string | null;
  status: RunStatus;
  verdict: RunVerdict | null;
  evidence: RunEvidence;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type RunListResponse = {
  items: RunResponse[];
};

export const runsApi = {
  list: () => api.get<RunListResponse>('/v1/runs'),
  get: (id: string) => api.get<RunResponse>(`/v1/runs/${id}`),
};
