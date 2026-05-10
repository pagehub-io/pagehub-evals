/**
 * Minimal API client to the pagehub-evals backend. Operator surfaces
 * call this; the SupportWidget uses its own (pagehub) base URL.
 */

import { tokenStorage } from '@/services/tokenStorage';

const API_BASE_URL =
  process.env.EXPO_PUBLIC_API_URL || 'http://localhost:8002';

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await tokenStorage.getItem('access_token');
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const r = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  if (!r.ok) {
    throw new Error(`${r.status} ${r.statusText}`);
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
