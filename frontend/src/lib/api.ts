import type {
  HealthResponse,
  FocusState,
  RemindersResponse,
  HAEntitiesResponse,
  CalendarEvent,
} from './types';

const BASE = process.env.NEXT_PUBLIC_ORCHESTRATOR_URL || '';

async function fetcher<T>(path: string, init?: RequestInit): Promise<T> {
  const url = path.startsWith('http') ? path : `${BASE}${path}`;
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

// Server-side fetcher (uses internal Docker URL)
export async function serverFetch<T>(path: string): Promise<T> {
  const serverBase = process.env.ORCHESTRATOR_URL || 'http://localhost:8888';
  const res = await fetch(`${serverBase}${path}`, { next: { revalidate: 30 } });
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}

export const api = {
  health: () => fetcher<HealthResponse>('/health'),
  focus: () => fetcher<FocusState>('/api/focus'),
  startFocus: (task: string, duration: number) =>
    fetcher<{ success: boolean }>('/api/focus/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task, duration }),
    }),
  stopFocus: () =>
    fetcher<{ success: boolean }>('/api/focus/stop', { method: 'POST' }),
  reminders: () => fetcher<RemindersResponse>('/api/reminders'),
  completeReminder: (id: string) =>
    fetcher<{ success: boolean }>(`/api/reminder/complete/${id}`, {
      method: 'POST',
    }),
  entities: () => fetcher<HAEntitiesResponse>('/api/ha/entities'),
  haCommand: (entityId: string, service: string, data?: Record<string, unknown>) =>
    fetcher<{ success: boolean }>('/api/ha/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: entityId, service, data }),
    }),
  calendarToday: () =>
    fetcher<{ events: CalendarEvent[] }>('/api/calendar/today'),
};
