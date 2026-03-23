import type {
  HealthResponse,
  FocusState,
  RemindersResponse,
  HAEntitiesResponse,
  CalendarEvent,
  TemperaturesResponse,
  ProgressToday,
  ProgressWeek,
  ProgressStreaks,
  AnnouncementEntry,
  AnnouncementStats,
  AmbientStatus,
  ShoppingItem,
  Conversation,
  SavedMessage,
} from './types';

const PROXY = '/api/proxy';

async function fetcher<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, init);
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
    fetcher<{ events: CalendarEvent[]; source?: string; count?: number }>('/api/calendar/today'),
  temperatures: () => fetcher<TemperaturesResponse>('/api/temperatures'),
  progressToday: () => fetcher<ProgressToday>('/api/progress/today'),
  progressWeek: () => fetcher<ProgressWeek>('/api/progress/week'),
  progressStreaks: () => fetcher<ProgressStreaks>('/api/progress/streaks'),
  announcementHistory: (limit = 20) =>
    fetcher<AnnouncementEntry[]>(`/api/announcements/history?limit=${limit}`),
  announcementStats: () => fetcher<AnnouncementStats>('/api/announcements/stats'),
  clearAnnouncements: () =>
    fetcher<{ ok: boolean; deleted: number }>('/api/announcements/history', {
      method: 'DELETE',
    }),
  ambientStatus: () => fetcher<AmbientStatus>('/api/ambient/status'),
  shoppingList: (listName?: string, includeChecked = false) =>
    fetcher<ShoppingItem[]>(
      `/api/shopping?include_checked=${includeChecked}${listName ? `&list_name=${listName}` : ''}`,
    ),
  addShoppingItem: (item: string, listName = 'grocery') =>
    fetcher<ShoppingItem>('/api/shopping', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item, list_name: listName }),
    }),
  checkShoppingItem: (id: number) =>
    fetcher<{ ok: boolean }>(`/api/shopping/${id}/check`, { method: 'POST' }),
  uncheckShoppingItem: (id: number) =>
    fetcher<{ ok: boolean }>(`/api/shopping/${id}/uncheck`, { method: 'POST' }),
  deleteShoppingItem: (id: number) =>
    fetcher<{ ok: boolean }>(`/api/shopping/${id}`, { method: 'DELETE' }),
  clearCheckedItems: (listName?: string) =>
    fetcher<{ ok: boolean; cleared: number }>(
      `/api/shopping/checked${listName ? `?list_name=${listName}` : ''}`,
      { method: 'DELETE' },
    ),
  // Chat conversations
  listConversations: (limit = 50) =>
    fetcher<Conversation[]>(`/api/chat/conversations?limit=${limit}`),
  createConversation: (title: string) =>
    fetcher<Conversation>('/api/chat/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    }),
  getConversationMessages: (convId: string) =>
    fetcher<{ conversation: Conversation; messages: SavedMessage[] }>(
      `/api/chat/conversations/${convId}/messages`,
    ),
  saveMessage: (convId: string, role: string, content: string, routing?: unknown, announcementType?: string) =>
    fetcher<SavedMessage>(`/api/chat/conversations/${convId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role, content, routing, announcement_type: announcementType }),
    }),
  updateConversation: (convId: string, title: string) =>
    fetcher<{ ok: boolean }>(`/api/chat/conversations/${convId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    }),
  deleteConversation: (convId: string) =>
    fetcher<{ ok: boolean }>(`/api/chat/conversations/${convId}`, { method: 'DELETE' }),
};
